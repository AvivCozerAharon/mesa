# mesa

Terminal pessoal de investimentos com dados públicos, briefing diário por IA e tese por posição.
Spec em `docs/superpowers/specs/2026-09-21-mesa-design.md`.

## Rodar
```bash
cp .env.example .env            # preencha MESA_SENHA e OPENAI_API_KEY
python -m mesa job fechamento   # preços (Yahoo) + métricas
python -m mesa job manha        # CVM, BCB, Tesouro, Treasury + métricas
python -m mesa                  # API + scheduler em http://localhost:8100
```
Carteira: `POST /posicoes` ou `POST /posicoes/importar` com um CSV como `dados_exemplo_carteira.csv`.
