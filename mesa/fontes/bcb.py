"""Séries do Banco Central (SGS): CDI diário (12), Selic diária (11), IPCA mensal (433).
CDI e Selic vêm em % ao dia; IPCA em % ao mês. Guardamos como vêm; quem capitaliza é `metricas`."""
from datetime import timedelta

import pandas as pd

from mesa.fontes import Coleta, Contexto, http_get

SERIES = {"CDI": 12, "SELIC": 11, "IPCA": 433}
URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados?formato=json&dataInicial={ini}&dataFinal={fim}"


def parse_serie(nome: str, itens: list[dict]) -> pd.DataFrame:
    if not itens:
        return pd.DataFrame()
    df = pd.DataFrame(itens)
    return pd.DataFrame({"nome": nome, "data": pd.to_datetime(df["data"], format="%d/%m/%Y").dt.date,
                         "valor": pd.to_numeric(df["valor"].str.replace(",", "."), errors="coerce")}).dropna()


class SGS:
    nome = "bcb"
    tabela = "benchmarks"

    def coletar(self, ctx: Contexto) -> tuple[pd.DataFrame, Coleta]:
        get = http_get(ctx)
        ini = (ctx.agora - timedelta(days=365 * ctx.anos_precos + 30)).strftime("%d/%m/%Y")
        fim = ctx.agora.strftime("%d/%m/%Y")
        partes, erros = [], []
        for nome, codigo in SERIES.items():
            try:
                r = get(URL.format(codigo=codigo, ini=ini, fim=fim))
                partes.append(parse_serie(nome, r.json()))
            except Exception as e:  # noqa: BLE001 - uma serie fora nao invalida as outras
                erros.append(f"{nome}: {e}")
        df = pd.concat([p for p in partes if not p.empty], ignore_index=True) if partes else pd.DataFrame()
        return df, Coleta(self.nome, not erros or not df.empty, erro="; ".join(erros) or None)
