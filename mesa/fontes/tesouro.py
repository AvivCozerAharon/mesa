"""Tesouro Direto (Tesouro Transparente): taxa e PU de todos os títulos por dia — serve de proxy
da curva de juros brasileira (o DI futuro é pago) e de preço para posições em Tesouro."""
import io
from datetime import timedelta

import pandas as pd

from mesa.fontes import Coleta, Contexto, http_get

URL = ("https://www.tesourotransparente.gov.br/ckan/dataset/df56aa42-484a-4a59-8184-7676580c81e3/resource/"
       "796d2059-14e9-44e3-80c9-2d9e30b405c1/download/PrecoTaxaTesouroDireto.csv")


def parse(texto: str, desde) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(texto), sep=";", decimal=",", dtype=str)
    df.columns = [c.strip() for c in df.columns]
    base = pd.to_datetime(df["Data Base"], format="%d/%m/%Y").dt.date
    df = df[base >= desde]
    if df.empty:
        return pd.DataFrame()
    num = lambda c: pd.to_numeric(df[c].str.replace(",", "."), errors="coerce")  # noqa: E731
    venc = pd.to_datetime(df["Data Vencimento"], format="%d/%m/%Y").dt.date
    return pd.DataFrame({
        "pais": "BR", "data": pd.to_datetime(df["Data Base"], format="%d/%m/%Y").dt.date.values,
        "vencimento": venc.values, "titulo": df["Tipo Titulo"].str.strip().values,
        "taxa": num("Taxa Compra Manha").values, "taxa_venda": num("Taxa Venda Manha").values,
        "pu": num("PU Base Manha").values,
    }).dropna(subset=["taxa"])


class TesouroDireto:
    nome = "tesouro"
    tabela = "curvas"

    def coletar(self, ctx: Contexto) -> tuple[pd.DataFrame, Coleta]:
        r = http_get(ctx)(URL)
        desde = (ctx.agora - timedelta(days=365 * ctx.anos_precos)).date()
        return parse(r.content.decode("latin-1"), desde), Coleta(self.nome, True)
