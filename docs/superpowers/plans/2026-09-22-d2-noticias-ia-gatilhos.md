# D2 — notícias, gatilhos, briefing por IA com validação e evals, e-mail

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** às 07:30 de 2026-09-23 a EC2 gera, sem intervenção, o briefing da carteira: notícias casadas por ativo, gatilhos disparados e, por posição, "a tese continua de pé?" — validado (só cita o que recebeu), com custo medido e evals rodando.

**Architecture:** `noticias.py` coleta RSS (Google News por termo de busca de cada posição + feeds gerais), dedupe por URL/título, casa com ativos por regras (ticker, nome, apelidos, exclusões) e grava no SQLite. `gatilhos.py` avalia regras declarativas sobre as métricas do dia e as notícias, com idempotência por hash de estado. `ia.py` monta a entrada (fatos + notícias numeradas + tese + gatilhos), chama a OpenAI com saída JSON, valida (citações existentes, números presentes nos fatos), reexecuta uma vez com a crítica, grava tokens/custo/latência. `evals/` mede casamento e validação sem rede, e a concordância da IA em casos rotulados quando há chave. `email.py` manda o resumo. O job da manhã encadeia tudo após `calcular`.

**Tech Stack:** feedparser, openai (SDK), smtplib, pytest.

**Spec:** `docs/superpowers/specs/2026-09-21-mesa-design.md` §5 (`noticias/`, `ia/`, `gatilhos/`, `evals/`), §8 (fluxo da manhã), §9 (falhas).

## Global Constraints

- A IA nunca recebe nada além de: fatos calculados (JSON), notícias numeradas (título/fonte/data/url), tese, gatilhos. Nunca recebe instrução de recomendar.
- Saída inválida não vai pra tela: fica `valido=0`, e a UI mostra só os fatos.
- Chave da OpenAI só por env; testes não batem na rede (cliente fake). Um teste `integration` opcional com chave.
- Tudo idempotente por dia: rodar o briefing duas vezes no mesmo dia não duplica nem cobra de novo (reaproveita se já existe válido e a entrada não mudou — hash da entrada).

---

### Task 1: `mesa/noticias.py` — coleta, dedupe e casamento

**Files:** Create `mesa/noticias.py`; Modify `mesa/armazenamento.py` (coluna `busca` em `posicoes` via migração `ALTER TABLE` se ausente; tabela `noticias` ganha `resumo`), `mesa/carteira.py` (`busca` na `Posicao`: termos de busca separados por `;`, default = ticker/identificador; para fundo = nome curto), `mesa/api.py` (aceita `busca` no CRUD); Test `tests/test_noticias.py`.

**Interfaces:**
- `termos(p: Posicao) -> list[str]` — `p.busca.split(";")` limpos; se vazio: ticker (B3/US), nome do fundo sem sufixos ("FIC FIM", "FUNDO DE INVESTIMENTO…") ou título do Tesouro.
- `coletar_rss(termos, fetch=feedparser.parse, max_por_termo=20) -> list[dict]` → `{url, titulo, fonte, publicada_em (ISO ou None), termo}`; URL canônica (sem `utm_*`, sem fragmento).
- `normalizar_titulo(t) -> str` (sem acento, minúsculas, sem pontuação).
- `casar(titulo: str, posicoes: list[Posicao], exclusoes: dict[str, list[str]] = EXCLUSOES) -> list[tuple[str, str, float]]` → `(identificador, metodo, score)`: `ticker` no título (score 1.0), `nome` (todos os tokens ≥ 4 letras do termo presentes, 0.8), apelido (0.8); exclusão: se o título contém uma frase de `EXCLUSOES[ident]` (ex.: VALE3 → "vale a pena", "vale do", "vale-tudo") e não contém o ticker → descarta.
- `salvar(db, itens, posicoes) -> dict(novas, casadas)` — `INSERT OR IGNORE` por URL; `noticia_ativo` com método/score.
- `recentes(db, identificador, dias=7) -> list[dict]` (id, titulo, fonte, publicada_em, url).
- `EXCLUSOES` embutido para os casos óbvios (VALE, OI, MULT, ITSA…); `FEEDS_GERAIS = [InfoMoney mercados, Valor (RSS público), Reuters Brasil]` — cada item casa com todos os ativos.

- [ ] Testes: canonicalização de URL; `casar("Vale a pena investir em CDB?", [VALE3]) == []`; `casar("Vale (VALE3) anuncia dividendos", [VALE3])` → método ticker; `casar("Petrobras muda política de preços", [PETR4 com busca "PETR4;Petrobras"])` → nome 0.8; `salvar` ignora URL repetida e título repetido de fonte diferente; `recentes` respeita janela.
- [ ] Commit `feat: noticias por rss com casamento por ativo`.

---

### Task 2: `mesa/gatilhos.py` — regras declarativas e disparos idempotentes

**Files:** Create `mesa/gatilhos.py`; Test `tests/test_gatilhos.py`; Modify `mesa/api.py` (`GET/POST/DELETE /gatilhos`, `GET /disparos?vistos=0`, `POST /disparos/{id}/visto`).

**Interfaces:**
- `REGRAS: dict[str, Callable[[dict m, dict params, list noticias], dict|None]]` — devolve `detalhe` quando dispara:
  `queda_desde_compra` (`m.vs_preco_medio_pct <= -params.pct`), `queda_1m` (`retornos.1m <= -pct`), `abaixo_mm50`, `abaixo_mm200`, `min_52s` (`s52.pct_do_fundo <= params.tolerancia_pct` default 1), `max_52s`, `vol_spike` (`vol_30d >= params.fator × vol_252d`, default 2), `drawdown` (`drawdown.atual <= -pct`), `noticia_contem` (`params.termos` em título, case-insensitive), `fundo_abaixo_bench` (`janelas_12m.pct < params.pct` default 50), `fundo_pct_bench_12m` (`pct_do_bench.12m < params.pct` default 90), `fundo_resgate` (`captacao_liquida_6m / pl <= -params.pct/100` default 20).
- `PADRAO = [("queda_desde_compra", {"pct": 15}), ("min_52s", {}), ("vol_spike", {}), ("noticia_contem", {"termos": ["recuperação judicial", "fraude", "investiga", "CVM abre processo"]}), ("fundo_abaixo_bench", {}), ("fundo_resgate", {})]` — criados por `garantir_padrao(db, posicao_id)` na primeira avaliação (uma vez; podem ser apagados).
- `avaliar(db, posicao_id, metricas, noticias, hoje) -> list[dict]` — para cada gatilho ativo da posição (ou global `posicao_id NULL`): calcula `detalhe`; `estado_hash = sha1(regra + json(params) + json(detalhe arredondado))`; `INSERT OR IGNORE` em `disparos`; devolve os **novos** disparos. Quando a condição deixa de valer, nada é registrado; quando volta, o hash muda (data do estado) → dispara de novo.
- `pendentes(db) -> list[dict]` (não vistos, com nome da posição).

- [ ] Testes: cada regra com um dict de métricas mínimo; idempotência (2ª avaliação → 0 novos); condição sai e volta → novo disparo; `garantir_padrao` não duplica.
- [ ] Commit `feat: gatilhos declarativos e idempotentes`.

---

### Task 3: `mesa/ia.py` — briefing com validação, custo e cache

**Files:** Create `mesa/ia.py`; Test `tests/test_ia.py` (cliente fake); Modify `mesa/config.py` (`openai_preco_in`, `openai_preco_out` US$ por 1M tokens, default 0 → custo só se configurado; `ia_max_posicoes_dia=30`).

**Interfaces:**
- `montar_entrada(posicao, tese, metricas, noticias, disparos, hoje) -> dict` — fatos **arredondados a 1 casa** e com unidades explícitas (`"retorno_12m_pct": 35.5`), notícias como `[{"id": "N1", "titulo", "fonte", "data"}]`, tese (texto + data), gatilhos disparados (`[{"regra", "detalhe"}]`). Sem URL (não precisa) e sem nada fora disso.
- `PROMPT_SISTEMA` (PT-BR): papel = analista que prepara o briefing do investidor; **proibido** recomendar comprar/vender/manter; só usar os fatos e notícias fornecidos; toda afirmação sobre notícia cita `[N#]`; se não há notícia nem gatilho, `tese_continua = "nao_avaliavel"`; saída JSON com o esquema: `{"situacao": str (≤ 90 palavras), "tese_continua": "sim|revisar|nao_avaliavel", "justificativa": str (≤ 60 palavras), "citacoes": [ids], "pontos_de_atencao": [str ≤ 3]}`.
- `validar(entrada, saida) -> tuple[bool, list[str]]` — JSON com as chaves e tipos certos; `citacoes ⊆ ids`; todo `[N#]` no texto está em `citacoes`; todo número com ≥ 3 dígitos ou casa decimal que aparece em `situacao/justificativa/pontos` existe no conjunto de números da entrada (fatos + títulos), com tolerância de arredondamento (1 casa); `tese_continua ∈ enum`; sem as palavras `compre|venda|comprar|vender|mantenha` em imperativo (regex) — o modelo não recomenda.
- `ClienteOpenAI(cfg)` com `completar(sistema, usuario) -> (texto, tokens_in, tokens_out, latencia_ms)` usando `chat.completions.create(model, messages, response_format={"type": "json_object"}, temperature=0.2)`; erro de rede/quota levanta `IAIndisponivel`.
- `gerar_briefing_posicao(cliente, db, posicao, entrada, hoje) -> dict` — hash da entrada; se já há briefing válido hoje com o mesmo hash, devolve o existente (sem chamar). Chama; valida; se inválido, uma segunda chamada com a lista de problemas; grava em `briefings` (`entrada`, `saida`, `modelo`, tokens, `custo_usd = in*preco_in/1e6 + out*preco_out/1e6`, latência, `valido`, `erro`). Devolve o registro.
- `gerar_briefing_carteira(cliente, db, resumo_carteira, briefings_posicoes, disparos, hoje) -> dict` — mesma mecânica; entrada = resumo + `tese_continua` de cada posição + disparos; saída `{"resumo": str ≤ 120 palavras, "olhar_hoje": [ativos], "citacoes": []}`.
- `briefing_do_dia(db, data) -> dict` (carteira + por posição) para a API.

- [ ] Testes: `validar` rejeita citação inexistente, número inventado (ex.: "caiu 37 %" quando os fatos têm 3,7), verbo imperativo, enum errado; aceita saída boa; cliente fake devolvendo inválido→válido na 2ª tentativa grava `valido=1` com 2 chamadas; cache por hash não chama de novo; `IAIndisponivel` grava `erro` e `valido=0`.
- [ ] Commit `feat: briefing por ia com validacao, custo e cache`.

---

### Task 4: evals

**Files:** Create `mesa/evals/__init__.py`, `mesa/evals/casos_casamento.json` (30 títulos reais/realistas × ativos esperados), `mesa/evals/casos_validacao.json` (12 saídas boas/ruins), `mesa/evals/casos_tese.json` (8 casos entrada→rótulo esperado, só com chave); `mesa/evals/__main__.py`; Test `tests/test_evals.py`.

**Interfaces:** `rodar_casamento() -> {"n", "precisao", "recall", "erros": [...]}`; `rodar_validacao() -> {"n", "acertos"}`; `rodar_tese(cliente) -> {"n", "concordancia", "validos_1a"}`; CLI grava `docs/evals.json` e imprime tabela; `POST /evals` opcional não (fica CLI). Registra em tabela `evals`.

- [ ] Commit `feat: evals de casamento, validacao e tese`.

---

### Task 5: e-mail, job da manhã completo, API, deploy

**Files:** Create `mesa/email.py` (`enviar(cfg, assunto, corpo_texto)`, no-op se SMTP não configurado); Modify `mesa/job.py` (`manha`: coletar → calcular → notícias → gatilhos → briefing → e-mail; cada etapa com try/except registrando em `coletas` como fonte `noticias|gatilhos|briefing|email`), `mesa/api.py` (`GET /briefing[?data=]`, `GET /noticias?posicao=&dias=`, gatilhos/disparos, `POST /job/briefing`), `README.md`; Test `tests/test_job_manha.py` (fake de fontes/cliente).

- [ ] Rodar local com chave (se ele já colocou) ou com cliente fake; deploy na EC2 (`git pull` + `up --build`); verificar `/briefing` e `/operacao`.
- [ ] Commit `feat: job da manha completo (noticias, gatilhos, briefing, email)` + push + deploy.

## Self-review

- Spec §5 `noticias/` ✔ T1, `gatilhos/` ✔ T2, `ia/` ✔ T3, `evals/` ✔ T4, e-mail e fluxo §8 ✔ T5. Falhas §9 (RSS fora, IA fora, saída inválida) cobertas em T3/T5.
- Nomes: `noticias.recentes(db, identificador)` usado por T3/T5; `gatilhos.avaliar` devolve novos disparos usados por T3 (entrada) e T5 (e-mail); `ia.gerar_briefing_posicao(cliente, db, posicao, entrada, hoje)` em T5. ✔
