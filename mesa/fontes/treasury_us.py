"""Curva de juros dos EUA (par yield curve) do Treasury.gov, CSV por ano. FRED bloqueia acesso
sem chave; o Treasury publica o mesmo dado sem cadastro."""
import io

import pandas as pd

from mesa.fontes import Coleta, Contexto, http_get

URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
       "{ano}/all?type=daily_treasury_yield_curve&field_tdr_date_value={ano}&page&_format=csv")
PRAZOS = {"1 Mo": 1 / 12, "1.5 Month": 1.5 / 12, "2 Mo": 2 / 12, "3 Mo": 0.25, "4 Mo": 4 / 12, "6 Mo": 0.5,
          "1 Yr": 1, "2 Yr": 2, "3 Yr": 3, "5 Yr": 5, "7 Yr": 7, "10 Yr": 10, "20 Yr": 20, "30 Yr": 30}


def parse(texto: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(texto))
    linhas = []
    for _, r in df.iterrows():
        d = pd.to_datetime(r["Date"]).date()
        for col, anos in PRAZOS.items():
            if col in df.columns and pd.notna(r[col]):
                linhas.append({"pais": "US", "data": d, "vencimento": anos, "titulo": col, "taxa": float(r[col]),
                               "taxa_venda": None, "pu": None})
    return pd.DataFrame(linhas)


class Treasury:
    nome = "treasury_us"
    tabela = "curvas"

    def coletar(self, ctx: Contexto) -> tuple[pd.DataFrame, Coleta]:
        get = http_get(ctx)
        anos = range(ctx.agora.year - ctx.anos_precos, ctx.agora.year + 1)
        partes = [parse(get(URL.format(ano=a)).text) for a in anos]
        df = pd.concat([p for p in partes if not p.empty], ignore_index=True) if partes else pd.DataFrame()
        return df, Coleta(self.nome, True)
