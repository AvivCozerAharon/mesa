# mesa

Terminal pessoal de investimentos: dados públicos, briefing diário por IA e **tese por posição**.
Feito porque eu invisto e as ferramentas boas custam milhares por mês; desenhado como produto —
qualquer pessoa roda com a própria carteira. Spec: `docs/superpowers/specs/2026-09-21-mesa-design.md`.

## O que ele responde de manhã

1. Como está a carteira (valor, P&L, pesos, exposição por moeda/classe) e o que mexeu.
2. Por posição: onde o preço está (52 semanas, drawdown, vol, médias, beta), retorno vs benchmark,
   fundamentos (múltiplos, margens, alavancagem, dividendos, últimos trimestres), notícias dos últimos
   7 dias casadas ao ativo, e **"a tese continua de pé?"** — avaliada por IA só com os fatos e notícias
   fornecidos, com citação obrigatória; a IA também descreve o que os fundamentos mostram, sem julgar caro/barato.
3. Gatilhos que **você** definiu (queda desde a compra, mínima de 52 s, pico de vol, notícia com termo
   sensível, fundo abaixo do benchmark, resgates no fundo…), idempotentes.
4. Fundos: retorno vs benchmark declarado, janelas móveis de 12 m, mediana dos pares da mesma classe
   CVM (≈400 por classe), custo, captação líquida.

O que ele **não** faz, por decisão: não diz "compre/venda". A IA descreve e checa a tese; a decisão é
do usuário. Saída da IA que cita notícia inexistente, inventa número ou recomenda é rejeitada pelo
validador e não vai pra tela.

## Fontes (todas públicas)

| dado | fonte | atraso |
|---|---|---|
| preços B3/EUA, índices, câmbio | Yahoo Finance (não oficial, atrás de adapter) | fechamento diário; cotação ~15 min |
| fundos: cota, PL, captação | CVM informe diário (36 meses, zip mensal) | D+1/D+2 |
| fundos: cadastro | CVM `registro_fundo_classe.zip` (regime RCVM 175) | diário |
| fundos: taxas e benchmark declarado | CVM lâmina | mensal |
| CDI, Selic, IPCA | BCB SGS | diário |
| curva BR (Tesouro Direto) | Tesouro Transparente | diário |
| curva EUA | Treasury.gov | diário |
| fundamentos de ações (P/L, EV/EBITDA, margens, ROE, dív. líq./EBITDA, DY, payout, crescimento, DRE trimestral) | Yahoo Finance | diário; balanço do trimestre anterior |
| notícias | Google News RSS por termo de busca de cada posição + feeds | contínuo |
| IA | OpenAI (modelo por env), saída JSON validada | por briefing |

## Interface

Uma página (`mesa/static/index.html`, sem framework), pensada como um caderno de teses: cada posição é
uma linha com a marca do dia na margem (✓ de pé, ? revisar, — sem elementos), e a mesma marca aparece
ao lado da tese que a IA conferiu. Tem faixa macro, gráfico com preço médio e média de 200, números,
fundamentos, notícias, gatilhos, o painel "Hoje" e a operação. `j`/`k` navega, `n` lança posição.
Em produção: http://<ec2>:8100 (senha única).

### Lançar posição

Buscar por nome em vez de decorar código: `/buscar` junta ticker (busca do Yahoo, filtrada para B3 e
bolsas americanas, com o tipo deduzido do sufixo — 11 é FII ou ETF, 31–39 é BDR), fundo por nome ou
CNPJ (47 mil classes e subclasses do cadastro CVM, maior PL primeiro) e título do Tesouro (das curvas
já baixadas). Cada linha mostra o identificador ao lado — ticker, CNPJ ou CNPJ + subclasse. Cada fonte
falha sozinha: sem Yahoo, fundo e Tesouro continuam; e termo que não pode ser ticker (CNPJ, "tesouro
ipca") nem chega a sair para a internet.

**Subclasses (RCVM 175).** A mesma classe publica uma cota por subclasse — taxas diferentes, cota
diferente. O identificador da posição vira `<cnpj>:<ID_SUBCLASSE>`, e a série de cotas é filtrada por
ele; pedir só o CNPJ devolve as linhas sem subclasse. A comparação com pares continua por CNPJ, com
uma subclasse por dia, porque ali só interessa a série.

Uma compra se lança por **data + quanto você pôs**; `/cotacao?data=` acha o preço daquele dia — cota no
informe diário da CVM (os zips mensais já estão em cache pelo job), PU na curva do Tesouro, fechamento
guardado ou, para papel novo, no Yahoo — e daí sai a quantidade. Dia sem pregão cai no anterior, e a UI
diz de que dia veio o preço.

### Plantão

Tela cheia (botão no topo ou `p`) para deixar num segundo monitor: as manchetes das últimas 48 horas em
ordem, marcando as que casaram com alguma posição, e os preços que importam — índices e câmbio, a curva
de juros brasileira e a americana com a variação em pontos-base no mês, e as suas posições. Atualiza a
cada dois minutos. É escuro mesmo no tema claro: ali se varre de longe, não se lê.

### Compras, não um número digitado

A posição é a soma dos seus aportes (tabela `compras`): quantidade é a soma e preço médio é a média
ponderada, recalculados a cada compra adicionada ou apagada. A data da posição é a da primeira compra.
Carteiras criadas antes disso migram sozinhas — cada posição vira a sua primeira compra.

## Rodar

```bash
python -m venv .venv && .venv/Scripts/pip install -e .[dev]
cp .env.example .env               # MESA_SENHA, OPENAI_API_KEY (opcional), SMTP (opcional)
python -m mesa job fechamento      # preços + métricas (≈ 5 s)
python -m mesa job manha           # CVM/BCB/Tesouro/Treasury + métricas + notícias + gatilhos + briefing + e-mail
python -m mesa                     # API + scheduler em http://localhost:8100 (07:00 manhã, 18:40 B3, 17:10 ET)
python -m mesa.evals [--com-ia]    # evals de casamento, validador e tese
```

Carteira: `POST /posicoes` (ou `POST /posicoes/importar` com um CSV como `dados_exemplo_carteira.csv`,
coluna `busca` = termos de notícia, ex.: `PETR4;Petrobras`). Docker: `docker compose up --build`.

**Fundos e "o que ele compra"**: cada posição aceita um `mandato` em texto livre (ex.: "debêntures
incentivadas de infraestrutura e CRIs, meta CDI+2%"). Ao salvar sem termos de busca, a IA extrai 3–6
termos ("debêntures incentivadas", "CRI infraestrutura", "saneamento básico"…) que passam a puxar as
notícias da posição, e o mandato entra no briefing como critério de relevância. Os termos ficam
editáveis; "Gerar termos com a IA" refaz.

**E-mail diário**: o job da manhã envia o resumo (carteira, gatilhos, tese e fundamentos por posição,
custo da IA) quando `MESA_SMTP_*` e `MESA_EMAIL_DESTINO` estão no `.env`. Com Gmail, use uma *senha de
app* (Conta Google → Segurança → Verificação em duas etapas → Senhas de app).

## Como é feito

- **Adapters de fonte** com contrato único (`coletar(ctx) -> (DataFrame, Coleta)`); erro vira registro
  em `coletas`, nunca derruba o job. Parquet imutável por (tabela, fonte, data) lido pelo DuckDB;
  SQLite para carteira, teses (com histórico), gatilhos, notícias, briefings, métricas.
- **Calendário por mercado** (B3 × NYSE, fechamento local, feriados 2024–27): toda data passa por ele.
- **Métricas** puras e testadas contra séries sintéticas; CDI capitalizado por dia útil.
- **IA com contrato**: entrada = só fatos arredondados + notícias numeradas + tese + gatilhos; saída
  JSON; validador cobra citações existentes, números presentes na entrada, sem verbo de recomendação;
  uma segunda tentativa com a crítica; custo/tokens/latência por briefing; mesma entrada no mesmo
  dia não chama de novo.
- **Evals** (`docs/evals.json`): casamento notícia↔ativo 33 casos — precisão 100 %, recall 100 %;
  validador 12/12; concordância da IA em "tese continua?" (8 casos rotulados) roda com `--com-ia`.

## O que quebrou na primeira semana (e virou regra)

1. O `cad_fi.csv` da CVM que todo tutorial usa mostra **99 % dos fundos como "CANCELADA"** — o
   cadastro migrou pro regime da Resolução 175. Fonte trocada para `registro_fundo_classe.zip`.
2. Tesouro e Treasury gravavam na mesma tabela com o mesmo nome de arquivo: um sobrescrevia o outro.
   Parquet passou a levar o nome da fonte.
3. Pares de fundos publicam cota com atraso diferente (D+1/D+2): sem `ffill` sobravam 7 pares de 400.
4. Tesouro Transparente devolveu 503 numa rodada; registrado, o resto seguiu; na seguinte voltou.
5. Yahoo entregou câmbio datado de "amanhã" (o dia vira na Ásia): o calendário por mercado descarta.
6. **"VOO" casou com notícia de helicóptero** — `voo` é palavra em português. Ticker só casa em
   maiúsculas no título original. Virou caso de eval.
7. Notícia repetida não era recasada quando a carteira ganhava termos novos: dedupe passou a recasar.
8. Reexecutar o job no mesmo dia chamava a IA de novo porque a entrada trazia só os gatilhos "novos
   da rodada"; agora traz os disparos do dia — reexecução é de graça.
9. `.env` copiado do Windows com CRLF (e uma aspa solta): a chave chegava ao container com `
"` no
   fim e a OpenAI devolvia 401. O job registrou o erro por posição e seguiu; `sed -i 's/
$//' .env` resolveu.

10. Yahoo entrega o `dividendYield` já em % (AAPL 0,32; PETR4 8,94) e eu "normalizei" 0,32 → 32 %.
    A IA repetiu "dividend yield 32 %" com toda a confiança: o validador garante coerência com a
    entrada, não verdade. Achado lendo o próprio briefing.
11. Para tickers da B3 o Yahoo devolve o `info` em BRL e a DRE em USD (Petrobras: 548 bi vs 89 bi/ano).
    A moeda da DRE agora é inferida pela razão receita 12 m / soma dos 4 trimestres e vai rotulada
    para a tela e para a IA.

## Limites conhecidos

- Open Finance não entra: puxar dados por lá exige ser instituição autorizada pelo Banco Central e estar
  no diretório de participantes (ou pagar um agregador). A carteira entra por CSV ou pela tela.

- **"Tese continua?" é uma leitura, não uma medição.** Mesma carteira, mesmo dia, conjuntos de notícias
  coletados em horas diferentes deram vereditos diferentes para VOO e PETR4. O que é estável: o
  validador (100 % das saídas obedeceram ao contrato) e os fatos numéricos. Por isso a justificativa
  sempre vem com as citações — o usuário confere em 10 segundos.
- Yahoo é não oficial; B3 é atrasada 15 min; fundos D+1/D+2; lâmina cobre poucos fundos (taxa fica "sem lâmina").
- Notícias só por título (Google News RSS); termos de nicho trazem itens antigos, que a janela de 7 dias descarta.
- Um usuário, uma senha; sem multiusuário.

