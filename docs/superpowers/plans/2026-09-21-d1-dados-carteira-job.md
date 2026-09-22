# D1 — fontes, armazenamento, calendário, carteira, métricas, job na EC2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ao fim do D1, `docker compose up` na EC2 coleta preços/benchmarks/fundos/juros todo dia, calcula as métricas da carteira real do autor e responde em `/carteira`.

**Architecture:** adapters de fonte com interface única gravam Parquet imutável por (fonte, data); DuckDB consulta os Parquet; SQLite guarda carteira/teses/coletas; calendário por mercado decide "último pregão"; métricas são funções puras testadas; API FastAPI com scheduler embutido; deploy no compose da EC2 que já existe.

**Tech Stack:** Python 3.12, pandas, pyarrow, duckdb, yfinance, requests, FastAPI/uvicorn, APScheduler, pytest, Docker.

**Spec:** `docs/superpowers/specs/2026-09-21-mesa-design.md`

## Global Constraints

- `dados/`, `mesa.db`, `.env` nunca entram no git. Carteira real só em `dados/`.
- Toda data de mercado passa por `mesa/calendario.py`; proibido `date.today()` fora dele.
- Adapter nunca lança para o job: devolve `Coleta(ok, linhas, erro)`; o job registra e segue.
- Parquet imutável: `dados/<fonte>/<AAAA-MM-DD>.parquet` (data da coleta); reexecutar sobrescreve o mesmo arquivo.
- Testes de adapter usam fixtures (sem rede); um teste marcado `integration` bate na rede.
- Commits pequenos; suíte verde antes de cada commit (`.venv/Scripts/python -m pytest -q -m "not integration"`).

---

### Task 1: esqueleto, config, calendário

**Files:** Create `pyproject.toml`, `mesa/__init__.py`, `mesa/config.py`, `mesa/calendario.py`, `mesa/feriados_b3.py`, `tests/test_calendario.py`, `.gitignore`, `pytest.ini`.

**Interfaces:**
- `Config` (dataclass frozen, `do_ambiente()`): `dados_dir="dados"`, `db_path="dados/mesa.db"`, `porta=8100`, `senha`, `openai_api_key`, `openai_modelo`, `fuso="America/Sao_Paulo"`, `smtp_*`, `email_destino`.
- `calendario.dias_uteis(mercado: "B3"|"NYSE", inicio: date, fim: date) -> list[date]`; `ultimo_pregao(mercado, agora: datetime) -> date` (se `agora` < fechamento do mercado, devolve o pregão anterior; fechamento B3 18:00 BRT, NYSE 16:00 ET); `eh_dia_util(mercado, d)`; `agora_brt()`; `FECHAMENTO = {"B3": (18, 0, "America/Sao_Paulo"), "NYSE": (16, 0, "America/New_York")}`.
- Feriados B3 2024–2027 embutidos (lista ANBIMA); NYSE 2024–2027 embutidos.

- [ ] Testes: `ultimo_pregao("B3", 2026-09-21 09:00 BRT) == 2026-09-18` (sexta), `("B3", 2026-09-21 18:30) == 2026-09-21`; `("NYSE", 2026-09-07 12:00 ET) == 2026-09-04` (Labor Day); 20/11/2026 (Consciência Negra, feriado B3 desde 2024) não é dia útil B3 mas é NYSE; `dias_uteis` exclui fim de semana e feriado.
- [ ] Implementar; passar; commit `feat: esqueleto, config e calendario por mercado`.

---

### Task 2: armazenamento (Parquet + DuckDB + SQLite)

**Files:** Create `mesa/armazenamento.py`, `tests/test_armazenamento.py`.

**Interfaces:**
- `gravar_parquet(dados_dir, fonte, data_coleta, df) -> Path` (cria pasta; sobrescreve).
- `Consulta(dados_dir)` com `duck()` (conexão DuckDB com views `precos`, `cotas_fundos`, `benchmarks`, `curvas` sobre `read_parquet(dir/fonte/*.parquet)`, deduplicadas por chave natural ficando a coleta mais recente: `QUALIFY row_number() over (partition by chave order by coletado_em desc) = 1`) e helpers `precos(ativos, inicio, fim) -> DataFrame`, `benchmark(nome, inicio, fim)`, `cotas(cnpjs, inicio, fim)`.
- `Db(db_path)` SQLite com `SCHEMA` (tabelas do spec §6) e `registrar_coleta(fonte, data_ref, linhas, duracao_ms, erro)`, `ultimas_coletas()`.
- Views toleram pasta vazia (devolvem 0 linhas).

- [ ] Testes: grava dois Parquet do mesmo ativo/data com valores diferentes → `precos` devolve o mais recente; pasta vazia não quebra; `registrar_coleta` + `ultimas_coletas`.
- [ ] Commit `feat: parquet imutavel + duckdb + sqlite`.

---

### Task 3: adapters de fonte

**Files:** Create `mesa/fontes/__init__.py` (`Coleta` dataclass, `Fonte` Protocol: `nome`, `coletar(ctx) -> tuple[DataFrame, Coleta]`), `mesa/fontes/yahoo.py`, `mesa/fontes/cvm.py`, `mesa/fontes/bcb.py`, `mesa/fontes/tesouro.py`, `mesa/fontes/treasury_us.py`; fixtures em `tests/fixtures/` (CSV pequeno de cada); `tests/test_fontes.py`.

**Interfaces (colunas de saída):**
- `yahoo.Yahoo(tickers_fn)`: `precos(ativo, mercado, data, abertura, maxima, minima, fechamento, ajustado, volume, fonte="yahoo", coletado_em)`. Mapeia `PETR4 → PETR4.SA` (mercado B3), `AAPL` (US), benchmarks `^BVSP, ^GSPC, BRL=X, IFIX.SA`. **Descarta linhas com data > último pregão do mercado** (o bug visto: `BRL=X` datado de amanhã). Também `cotacao_atual(ativos) -> dict` via `fast_info` para a UI (cache 60 s).
- `cvm.InformeDiario(cnpjs_fn, meses=36)`: baixa `inf_diario_fi_AAAAMM.zip` (cache do zip em `dados/cvm/raw/`, rebaixa só o mês corrente), filtra CNPJs da carteira **e** pares (mesma classe, via `cad_fi.csv`, PL > 50 M) → `cotas_fundos(cnpj, data, cota, pl, captacao, resgate, cotistas)`. `cvm.Cadastro()` → `fundos(cnpj, nome, classe, gestor, situacao, publico_alvo)`; `cvm.Lamina()` → `taxas(cnpj, taxa_adm, taxa_perf, benchmark_declarado)`.
- `bcb.SGS()`: séries 12 (CDI diário), 11 (Selic), 433 (IPCA mensal) → `benchmarks(nome, data, valor)`; CDI vem em % ao dia → guardar como está e capitalizar nas métricas.
- `tesouro.TesouroDireto()`: CSV completo → `curvas(pais="BR", data, vencimento, taxa, tipo_titulo, preco)`.
- `treasury_us.Treasury()`: CSV do Treasury.gov (par yield curve) → `curvas(pais="US", data, vencimento(prazo em anos), taxa)`; se falhar, tenta FRED CSV.

- [ ] Testes com fixtures: cada adapter parseia sua fixture nas colunas certas; Yahoo descarta data futura; CVM filtra por CNPJ e pares; erro de rede vira `Coleta(ok=False, erro=…)`. Um teste `integration` por fonte (rede real, 5 linhas).
- [ ] Commit `feat: adapters yahoo, cvm, bcb, tesouro, treasury`.

---

### Task 4: carteira (SQLite CRUD + CSV + P&L)

**Files:** Create `mesa/carteira.py`, `tests/test_carteira.py`, `dados/carteira_exemplo.csv` (commitado; carteira fictícia).

**Interfaces:**
- `Posicao(id, ativo, tipo, mercado, identificador, quantidade, preco_medio, moeda, data_compra, ativa=True)`; `Carteira(db)`: `criar(p, tese=None)`, `atualizar(id, **campos)`, `excluir(id)` (soft: `ativa=False`), `listar(ativas=True)`, `tese(id) -> str | None`, `historico_teses(id)`, `definir_tese(id, texto)` (insere nova linha), `importar_csv(path) -> int`.
- Validação: ticker B3 `^[A-Z]{4}\d{1,2}$`, US `^[A-Z.]{1,6}$`, CNPJ 14 dígitos válido, Tesouro `"Tesouro IPCA+ 2035"`; quantidade > 0; preço > 0.
- `valorizar(carteira, consulta, cambio) -> DataFrame` com `valor_brl, custo_brl, pnl_brl, pnl_pct, peso, moeda, classe, data_preco, idade_dias` por posição e totais por moeda/classe.

- [ ] Testes: CRUD; tese com histórico; CSV com linha inválida → erro apontando a linha; `valorizar` converte USD e calcula peso; posição sem preço → `valor_brl=None` e `idade_dias=None` (não some da lista).
- [ ] Commit `feat: carteira com CRUD, teses e valorizacao`.

---

### Task 5: métricas

**Files:** Create `mesa/metricas.py`, `tests/test_metricas.py` (séries sintéticas com resultados conhecidos).

**Interfaces (todas puras, entrada `pd.Series` indexada por data):**
- `retorno(serie, inicio, fim)`, `retorno_periodos(serie, hoje) -> {"1m","3m","6m","12m","ytd","desde_compra"}` (com `data_compra` opcional).
- `cdi_acumulado(cdi_diario, inicio, fim)` (capitaliza `(1+r/100)` por dia útil B3).
- `drawdown(serie) -> {"maximo", "atual", "dias_recuperacao_max", "em_drawdown_desde"}`.
- `vol(serie, janela)` anualizada (√252), `media_movel(serie, n)`, `distancia_52s(serie) -> {"pct_do_topo", "pct_do_fundo", "topo", "fundo"}`.
- `beta(serie, bench, janela=252)`, `janelas_moveis(serie, bench, meses=12) -> {"n", "batidas", "pct"}`.
- `contribuicao_risco(retornos: DataFrame, pesos) -> Series`.
- Fundos: `metricas_fundo(cota, bench, pares: DataFrame, taxa_adm, taxa_perf, pl, captacao) -> dict` (retornos 12/24/36 m vs bench, janelas, mediana dos pares e percentil, custo anual estimado, captação líquida 6 m, drawdown).
- `metricas_posicao(precos, bench, data_compra, preco_medio) -> dict` agrega tudo para uma ação/ETF.

- [ ] Testes: série que sobe 1 %/dia por 10 dias → retorno conhecido; CDI 0,05 %/dia × 21 dias úteis → 1,0555 %; drawdown de série `[100,120,90,100,130]` = −25 %, recuperou; janelas móveis com bench constante; beta de série = 2×bench é 2.
- [ ] Commit `feat: metricas de posicao e de fundo`.

---

### Task 6: job diário + API + compose + deploy na EC2

**Files:** Create `mesa/job.py`, `mesa/api.py`, `mesa/__main__.py`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `scripts/deploy_ec2.sh`, `README.md` (mínimo), `tests/test_api.py`, `tests/test_job.py`.

**Interfaces:**
- `job.coletar_tudo(cfg, fontes, db, agora)`: para cada fonte, `coletar` → Parquet + `registrar_coleta`; nunca lança; devolve resumo. `job.calcular(cfg, db, consulta, agora)` → grava `metricas_diarias(data, posicao_id, json)` no SQLite (tabela nova). `job.manha(cfg)` = coletar (CVM, BCB, Tesouro, Treasury) + calcular; `job.fechamento(cfg)` = coletar Yahoo + calcular.
- Scheduler (APScheduler `BackgroundScheduler`, fuso BRT): `manha` 07:00, `fechamento` 18:40, `fechamento_us` 18:15 BRT (após 17:00 ET, exceto horário de verão — usar cron em ET: 17:10 America/New_York).
- API: `GET /saude`, `GET /carteira` (valorizada + métricas mais recentes), `GET/POST/PUT/DELETE /posicoes[/id]`, `POST /posicoes/importar` (CSV), `GET/POST /posicoes/{id}/tese`, `GET /ativo/{id}` (série 5 anos, médias, compra, métricas), `GET /operacao` (últimas coletas, erros), `POST /job/{manha|fechamento}` (dispara manualmente, protegido), senha via cookie (`POST /login`), `GET /` placeholder de UI (D3).
- `python -m mesa` = API + scheduler; `python -m mesa job manha|fechamento` = roda e sai.
- Compose: serviço `mesa` (porta 8100, volume `./dados:/app/dados`, `env_file: .env`, `restart: unless-stopped`). `scripts/deploy_ec2.sh`: `git clone/pull` em `/opt/mesa`, cria `.env` se não existir (avisa para preencher), `docker compose up --build -d`. Abrir porta 8100 no SG (Terraform do samu-sim: variável `portas_extras` ou regra manual — anotar).

- [ ] Testes: job com fonte que lança → `coletas` registra erro e as outras rodam; API CRUD via `TestClient`; `/carteira` com Parquet de fixture.
- [ ] Rodar local `python -m mesa job manha` e `fechamento` com a carteira real (em `dados/`), conferir `/carteira`.
- [ ] Deploy na EC2; `curl http://IP:8100/saude`; `POST /job/fechamento`; conferir `/operacao` e `/carteira` → **critério de pronto do D1**.
- [ ] Commit `feat: job diario, api, compose e deploy` + push (repo público `mesa`, sem dados).

## Self-review

- Spec §3 (escopo D1): fontes ✔ (T3), carteira CRUD ✔ (T4 + API T6), métricas ✔ (T5), job na EC2 ✔ (T6). Notícias/IA/gatilhos/UI = D2/D3.
- Nomes: `Consulta.precos/benchmark/cotas` usados por T4/T5/T6; `Coleta` em T3/T6; `metricas_posicao`/`metricas_fundo` em T5/T6. ✔
- Risco: CVM 36 zips na EC2 (≈ 1 GB tráfego, 10 min) — fazer o primeiro carregamento local e subir os Parquet via `scp` se demorar; job só rebaixa o mês corrente.
