# mesa

Terminal pessoal de investimentos: dados públicos, briefing diário por IA e **tese por posição**.
Feito porque eu invisto e as ferramentas boas custam milhares por mês; desenhado como produto —
qualquer pessoa roda com a própria carteira. Spec: `docs/superpowers/specs/2026-09-21-mesa-design.md`.

## O que ele responde de manhã

1. Como está a carteira (valor, P&L, pesos, exposição por moeda/classe) e o que mexeu.
2. Por posição: onde o preço está (52 semanas, drawdown, vol, médias, beta), retorno vs benchmark,
   notícias dos últimos 7 dias casadas ao ativo, e **"a tese continua de pé?"** — avaliada por IA
   só com os fatos e notícias fornecidos, com citação obrigatória.
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
| notícias | Google News RSS por termo de busca de cada posição + feeds | contínuo |
| IA | OpenAI (modelo por env), saída JSON validada | por briefing |

## Interface

Uma página (`mesa/static/index.html`, sem framework): faixa macro, carteira com o veredito de tese por
posição, gráfico com preço médio e média de 200, números, briefing e tese editável (com histórico),
notícias, gatilhos, painel "Hoje" e operação. `j`/`k` navega. Em produção: http://<ec2>:8100 (senha única).

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
9. `.env` copiado do Windows com CRLF (e uma aspa solta): a chave chegava ao container com `"` no
   fim e a OpenAI devolvia 401. O job registrou o erro por posição e seguiu; `sed 's/$//'` resolveu.

## Limites conhecidos

- **"Tese continua?" é uma leitura, não uma medição.** Mesma carteira, mesmo dia, conjuntos de notícias
  coletados em horas diferentes deram vereditos diferentes para VOO e PETR4. O que é estável: o
  validador (100 % das saídas obedeceram ao contrato) e os fatos numéricos. Por isso a justificativa
  sempre vem com as citações — o usuário confere em 10 segundos.
- Yahoo é não oficial; B3 é atrasada 15 min; fundos D+1/D+2; lâmina cobre poucos fundos (taxa fica "sem lâmina").
- Notícias só por título (Google News RSS); termos de nicho trazem itens antigos, que a janela de 7 dias descarta.
- Um usuário, uma senha; sem multiusuário.

