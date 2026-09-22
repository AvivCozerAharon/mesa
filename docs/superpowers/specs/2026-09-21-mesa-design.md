# mesa — spec de design

*Terminal pessoal de investimentos com dados públicos, briefing diário por IA e tese por posição.*
Data: 2026-09-21. Prazo: rodando em produção em 2026-09-22, completo em 2026-09-23, ensaio 24, entrevista 25.

## 1. Problema e enquadramento

Investidor pessoa física no Brasil não tem ferramenta entre a planilha e o terminal profissional
(Bloomberg ≈ US$ 25 mil/ano; Economatica/Quantum/Comdinheiro R$ 300–3 000/mês). O que essas ferramentas
vendem é, em grande parte, **engenharia sobre dados públicos** (B3, CVM, BCB, Tesouro, SEC/Yahoo) mais
método. O `mesa` é a ferramenta que o autor usa todo dia de manhã, desenhada como produto: qualquer pessoa
roda com a própria carteira.

Frase de apresentação: *"fiz pra resolver o meu problema — eu invisto e as ferramentas boas custam
milhares por mês. Desenhei como produto, não como script; o usuário nº 1 sou eu, todo dia de manhã."*

## 2. O que o mesa responde (e o que não faz)

**Responde, de manhã, em 2 minutos:**

1. Como está minha carteira hoje (valor, P&L, peso, exposição por moeda/classe) e o que mexeu ontem.
2. Para cada posição: onde o preço está (52 semanas, drawdown, vol, médias), o que saiu de notícia
   (com link) e **se a minha tese continua de pé** — avaliada por IA contra fatos e notícias fornecidos,
   com citação obrigatória.
3. Quais gatilhos que **eu** defini dispararam (queda desde a compra, cruzamento de média, mínimo de 52
   semanas, notícia com termo, fundo abaixo do benchmark em N janelas).
4. Para fundos: está pagando a taxa que cobra? (vs benchmark declarado, vs mediana dos pares, em janelas
   móveis; custo; captação; drawdown.)
5. Macro: curva de juros BR e EUA hoje vs 1 mês, dólar, inflação, Selic/Fed — o pano de fundo.

**Não faz, por decisão:**

- Não diz "compre/venda". A IA não opina sobre ativo; ela organiza fatos, checa a tese e aponta o que
  mudou. A decisão é do usuário. Motivos: o modelo não tem informação que o mercado não tem; opinião sem
  fonte não é auditável; e é a regra que separa ferramenta de decisão de "chute com confiança".
- Não finge tempo real onde não há: EUA pode ser tempo real (fora do escopo do dia 25), BR é atrasado
  15 min, fundos são D+1/D+2, CVM mensal. O painel mostra a idade de cada dado.
- Não executa ordens, não conecta em corretora.

## 3. Escopo do dia 25

| entra | fica para depois |
|---|---|
| Carteira com CRUD na UI (criar, editar, excluir posição e tese) + importação CSV: ações, FIIs, ETFs e BDRs da B3; ações/ETFs dos EUA; fundos CVM; Tesouro Direto | importação de nota de corretagem / open finance |
| Preços diários (histórico 5 anos) + cotação atrasada 15 min | WebSocket tempo real (Alpaca/Finnhub) |
| Benchmarks: CDI, Selic, IPCA, IBOV, S&P 500, USD/BRL, IFIX | Tesouro intradiário |
| Fundos: cota/PL/captação (CVM informe diário), cadastro, taxas (lâmina), pares por classe | composição da carteira do fundo (CDA) |
| Notícias: RSS por ativo (Google News RSS + feeds nacionais), dedupe, casamento com ativo | busca web ativa |
| Briefing diário por IA com evals, citação obrigatória e custo medido | chat livre com a carteira |
| Gatilhos configuráveis, idempotentes; aviso na UI e por e-mail | Telegram/WhatsApp |
| UI estilo terminal: carteira, ativo, macro, notícias, briefing | app mobile |
| Job diário na EC2 (compose), log de operação, README com medições | multiusuário, autenticação além de senha única |

## 4. Fontes de dados (verificadas em 2026-09-21)

| dado | fonte | cadência | observação |
|---|---|---|---|
| Preços B3 e EUA, índices, câmbio | Yahoo Finance via `yfinance` (não oficial) | diário + cotação ~15 min | adapter com interface própria; fallback `brapi.dev` (token grátis) para B3. Risco assumido: API não oficial pode mudar — por isso o adapter e o cache em Parquet |
| Fundos: cota, PL, captação, resgate, cotistas | CVM `dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_AAAAMM.zip` | mensal (mês corrente atualizado diariamente) | 36 meses ≈ 36 zips de ~30 MB; só fundos da carteira + pares da mesma classe entram no Parquet |
| Fundos: cadastro (classe, gestor, situação, público) | CVM `cad/DADOS/cad_fi.csv` | diário | |
| Fundos: taxas de adm/perf | CVM lâmina `lamina_fi_AAAAMM.zip` | mensal | |
| CDI, Selic, IPCA | BCB SGS API (`bcdata.sgs.12/11/433`) | diário/mensal | CDI capitalizado por dia útil |
| Curva de juros BR | Tesouro Transparente `PrecoTaxaTesouroDireto.csv` (taxas dos títulos por vencimento) | diário | proxy da curva; DI futuro é pago |
| Curva de juros EUA | FRED (`DGS1M…DGS30`) | diário | |
| Feriados B3 | ANBIMA (CSV) + lista embutida | anual | |
| Notícias | Google News RSS (`news.google.com/rss/search?q=…&hl=pt-BR`), Valor, InfoMoney, Reuters | contínuo | só título/link/data/fonte; sem scraping do corpo |
| IA | OpenAI API (modelo configurável por env, saída estruturada JSON) | por briefing | chave só em `.env` (`OPENAI_API_KEY`), nunca no git |

## 5. Arquitetura

```
 fontes ──▶ ingest (adapters) ──▶ Parquet por fonte/dia ──▶ DuckDB (consultas)
                                                          │
 carteira/teses/gatilhos/briefings (SQLite) ◀── API FastAPI ──▶ UI (HTML+JS, estilo terminal)
                                                          │
 scheduler (APScheduler na API): 18:40 preços BR/EUA · 07:00 CVM/BCB/Tesouro/FRED ·
 07:20 notícias · 07:30 métricas + gatilhos + briefing IA + e-mail
```

Componentes (`mesa/`):

- `fontes/` — um adapter por fonte, todos com a mesma interface `coletar(desde) -> DataFrame` e
  `nome`, `idade_maxima`. Cada coleta grava Parquet imutável em `dados/<fonte>/<data>.parquet` e um
  registro em `coletas` (SQLite) com linhas, duração, erro. Reexecutar é idempotente (sobrescreve o dia).
- `armazenamento/` — DuckDB lê os Parquet direto (`read_parquet('dados/precos/*.parquet')`); views
  `precos`, `cotas_fundos`, `benchmarks`, `curvas`. SQLite (`mesa.db`) para o que é transacional:
  `posicoes`, `teses`, `gatilhos`, `disparos`, `briefings`, `noticias`, `coletas`, `evals`.
- `calendario/` — dias úteis B3 e NYSE, conversão de fuso (America/Sao_Paulo, America/New_York),
  `ultimo_pregao(mercado, data)`. Toda métrica passa por aqui; nunca por `date.today()` solto.
- `metricas/` — funções puras sobre DataFrames: retorno por período, retorno vs benchmark, drawdown
  (máximo, atual, dias para recuperar), vol (30 d, 252 d), médias 50/200, distância 52 s, beta,
  janelas móveis, contribuição de risco (covariância) — e para fundos: janelas de 12 m vs benchmark,
  mediana dos pares (mesma classe CVM, PL > R$ 50 M, ≥ 36 m de cota), custo (taxa × PL médio),
  captação líquida 6 m. Testadas contra valores conhecidos (5 fundos cuja lâmina publica o número).
- `carteira/` — posições (ativo, tipo, mercado, quantidade, preço médio, data, moeda, tese), P&L em
  BRL (conversão pelo câmbio do dia), pesos, exposição. Importação CSV com validação.
- `noticias/` — coleta RSS, dedupe (URL canônica + título normalizado), **casamento com ativo** por
  ticker, razão social e apelidos (`VALE3`, `Vale`, `Vale S.A.`), com lista de exclusão ("vale a pena").
  Cada notícia guarda `fonte, url, titulo, data, ativos[]`.
- `ia/` — briefing por posição e da carteira. Entrada: **só** fatos calculados (JSON), notícias
  numeradas dos últimos 7 dias, tese e gatilhos. Saída estruturada: `situacao` (1 parágrafo),
  `tese_continua` (`sim|revisar|nao_avaliavel` + justificativa), `citacoes` (ids), `pontos_de_atencao`.
  Validador rejeita saída que cita id inexistente ou afirma número que não está nos fatos
  (checagem por regex dos números). Sem notícia e sem gatilho → `nao_avaliavel`, não inventa.
  Registra tokens, custo e latência por briefing. Prompt cacheado (fatos e regras fixas primeiro).
- `gatilhos/` — regras por posição (`queda_desde_compra >= 15`, `cruzou_mm50_para_baixo`,
  `min_52s`, `vol_30d > 2x vol_252d`, `noticia_contem: "recuperação judicial"`, `fundo_abaixo_bench_janelas >= 8/12`).
  Avaliadas após as métricas; `disparos` guarda `(posicao, regra, data_estado)` — a mesma condição
  não dispara de novo até sair e voltar (idempotência).
- `api/` — FastAPI: `/carteira`, `/posicoes` (CRUD + importação), `/ativo/{id}`, `/macro`,
  `/noticias`, `/briefing/{data}`, `/gatilhos`, `/operacao` (coletas, erros, custo de IA), `/saude`.
  Senha única via env (`MESA_SENHA`) em cookie; SG da EC2 restrito ao IP do autor.
- `ui/` — uma página, densa, escura, estilo terminal (fonte mono para números, painéis): faixa macro no
  topo, tabela da carteira, painel do ativo (gráfico de candles/linha com médias e marcas de compra,
  métricas, notícias, briefing), aba de fundos vs pares, aba de operação. Gráficos com
  `lightweight-charts` (CDN). Atalhos de teclado para trocar de ativo.
- `evals/` — 30+ casos rotulados: (a) casamento notícia↔ativo (precisão/recall), (b) saída da IA
  cumpre o contrato (100 % citações válidas, 0 números inventados), (c) `tese_continua` concorda com o
  rótulo humano em casos construídos. `python -m mesa.evals` imprime tabela e grava `docs/evals.json`.

## 6. Modelo de dados (SQLite)

- `posicoes(id, ativo, tipo{acao,fii,etf,bdr,acao_us,etf_us,fundo,tesouro}, mercado{B3,US,CVM,TD}, identificador{ticker|CNPJ|título}, quantidade, preco_medio, moeda, data_compra, ativa)`
- `teses(posicao_id, texto, criada_em, atualizada_em)` — histórico (nunca sobrescreve)
- `gatilhos(id, posicao_id|null, regra, parametros json, ativo)` · `disparos(gatilho_id, data, estado_hash, visto)`
- `noticias(id, url, titulo, fonte, publicada_em, coletada_em)` · `noticia_ativo(noticia_id, ativo, metodo, score)`
- `briefings(id, data, posicao_id|null, entrada json, saida json, modelo, tokens_in, tokens_out, custo_usd, latencia_ms, valido)`
- `coletas(fonte, data_ref, linhas, duracao_ms, erro, executada_em)` · `evals(data, suite, casos, acertos, detalhe json)`

Parquet: `precos(ativo, mercado, data, abertura, maxima, minima, fechamento, ajustado, volume, fonte)`,
`cotas_fundos(cnpj, data, cota, pl, captacao, resgate, cotistas)`, `benchmarks(nome, data, valor)`,
`curvas(pais, data, vencimento, taxa)`.

## 7. Decisões (ADRs)

1. **IA não recomenda; checa a tese.** Alternativa rejeitada: "sugestão de manter/vender". Motivo:
   auditabilidade e honestidade epistêmica; a tese do usuário é o que dá ao modelo um critério objetivo.
2. **Dados públicos + adapter por fonte.** Alternativa: API paga unificada (R$ 150+/mês). Motivo: o
   projeto existe porque a ferramenta paga é cara; o custo é fragilidade de fonte não oficial, mitigado
   por Parquet imutável (histórico nunca some) e fallback.
3. **Parquet + DuckDB para séries; SQLite para o transacional.** Alternativa: Postgres para tudo.
   Motivo: séries são append-only e colunares; uma t3.micro lê 5 anos × 200 ativos em ms; zero admin.
4. **Job na própria API (APScheduler), um container.** Alternativa: cron do host / Lambda. Motivo:
   um processo, um log, deploy com o compose que já existe na EC2; escala não é problema aqui.
5. **Calendário explícito por mercado.** Motivo: "ontem" não é o mesmo dia útil em B3 e NYSE; feriado
   brasileiro com bolsa americana aberta é o bug clássico.
6. **Saída da IA estruturada e validada.** Alternativa: texto livre. Motivo: contrato testável
   (citações, números), evals possíveis, custo previsível.
7. **Senha única + SG por IP.** Alternativa: OAuth. Motivo: um usuário; dado sensível (carteira)
   exige *alguma* barreira; o resto é escopo futuro.
8. **Atrasado 15 min é dito na tela.** Cada painel mostra a idade do dado. Motivo: confiança.

## 8. Fluxos

**Manhã (07:00–07:35, America/Sao_Paulo):**
1. Coleta CVM (mês corrente), BCB, Tesouro, FRED, feriados. Falha em uma fonte → registra em `coletas`,
   marca idade do dado, segue.
2. Coleta notícias (RSS) → dedupe → casamento com ativos da carteira.
3. Recalcula métricas por posição e carteira (dados até o último pregão de cada mercado).
4. Avalia gatilhos → novos `disparos`.
5. Gera briefing por posição (só as que têm notícia nova, gatilho ou mudança relevante — as outras
   reaproveitam o de ontem com nota "sem novidade") e o resumo da carteira; valida; grava com custo.
6. Envia e-mail (SMTP) com o resumo e link. Tudo logado em JSON.

**Fim do pregão (18:40 BRT / 17:10 ET):** preços do dia para todos os ativos da carteira e benchmarks.

**Usuário abre a UI:** carteira com cotações ~15 min (busca ao vivo, cache 60 s), clica no ativo,
lê briefing e notícias, edita tese, cria gatilho, marca disparo como visto.

## 9. Falhas previstas e resposta

| falha | resposta |
|---|---|
| Yahoo muda/bloqueia | adapter devolve erro tipado; fallback brapi para B3; histórico em Parquet intacto; painel mostra "preços de D-1" |
| CVM atrasa o informe | cota D-2/D-3 aceita; idade exibida; gatilhos de fundo usam a última data disponível |
| RSS fora | briefing roda sem notícias e diz isso (`nao_avaliavel` quando só havia notícia como insumo) |
| IA fora / cota estourada | briefing do dia fica "pendente", métricas e gatilhos saem normalmente; retry 3× com backoff |
| saída da IA inválida | rejeitada, uma repetição com a validação no prompt; se falhar de novo, marca `valido=false` e mostra só os fatos |
| feriado num mercado só | calendário resolve `ultimo_pregao` por mercado; carteira mistura datas e diz qual é qual |
| job cai no meio | cada etapa é idempotente por (fonte, data); reexecução completa o que faltou |
| desdobramento/grupamento | usa preço ajustado do Yahoo para métricas; preço médio da posição é do usuário — aviso na UI quando há evento corporativo detectado (salto > 30 % no não ajustado sem salto no ajustado) |

## 10. Medições que o README vai ter

- Evals: precisão/recall do casamento notícia↔ativo; % de briefings válidos na 1ª tentativa; custo
  médio por briefing (tokens/US$) e por manhã; latência.
- Operação: dias rodando, coletas com erro por fonte, tempo do job da manhã.
- Métricas validadas: 5 fundos com retorno/vol/drawdown conferidos com a lâmina (diferença < 0,1 p.p.).
- Um bug de dado encontrado por olhar o próprio briefing (vai existir).

## 11. Cronograma

| dia | entrega | critério de pronto |
|---|---|---|
| 21 (D1) | repo, adapters (Yahoo, CVM, BCB, Tesouro, FRED, feriados), Parquet/DuckDB, calendário, carteira (SQLite + CSV + API), métricas com testes, **job diário na EC2 rodando** | `docker compose up` na EC2 coleta e calcula; `/carteira` responde com a carteira real do autor |
| 22 (D2) | notícias + casamento, briefing IA com validação e evals, gatilhos, e-mail; **primeiro briefing real de manhã** | briefing do dia 23 gerado às 07:30 sem intervenção |
| 23 (D3) | UI terminal (carteira, ativo, macro, fundos vs pares, operação), README com medições | autor usa de manhã; screenshot no README |
| 24 | ensaio; só correções de operação | — |

## 12. Riscos

- Tempo: se D1 atrasar, a UI encolhe (tabela + gráfico + briefing) — os dados e o job não encolhem.
- Fontes não oficiais (Yahoo): mitigado (ADR 2); documentado como limite.
- Carteira real no repositório: **nunca**. `dados/`, `mesa.db`, `.env` no `.gitignore`; README usa
  carteira de exemplo.
- Tentação de a IA "opinar": o validador e o prompt proíbem; o eval (c) mede.
