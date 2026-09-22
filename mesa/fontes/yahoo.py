"""Preços diários e cotação recente via Yahoo Finance (yfinance — API não oficial).

Decisão (ADR 2): fonte grátis e não oficial, isolada atrás deste adapter; o histórico fica em
Parquet e não depende de a API continuar existindo. Descarta linhas datadas depois do último pregão
do mercado: o Yahoo devolve câmbio datado de "amanhã" porque o dia vira na Ásia.
"""
from datetime import timedelta

import pandas as pd

from mesa.calendario import ultimo_pregao
from mesa.fontes import Coleta, Contexto

BENCHMARKS = {"IBOV": ("^BVSP", "B3"), "SP500": ("^GSPC", "NYSE"), "USDBRL": ("BRL=X", "FX"),
              "IFIX": ("IFIX.SA", "B3"), "NASDAQ": ("^IXIC", "NYSE")}


def simbolo_yahoo(ativo: str, mercado: str) -> str:
    if mercado == "B3":
        return f"{ativo}.SA"
    return ativo


def mercado_calendario(mercado: str) -> str:
    return "B3" if mercado == "B3" else "NYSE"


def _baixar(simbolos: list[str], inicio, fim, download=None) -> pd.DataFrame:
    if download is None:
        import yfinance as yf
        download = yf.download
    df = download(simbolos, start=inicio, end=fim, progress=False, auto_adjust=False, group_by="ticker", threads=True)
    return df


def normalizar(df: pd.DataFrame, simbolos: dict[str, tuple[str, str]], ctx: Contexto) -> pd.DataFrame:
    """De `yf.download(group_by='ticker')` para as colunas de `precos`. `simbolos` = símbolo → (ativo, mercado)."""
    linhas = []
    if df is None or df.empty:
        return pd.DataFrame()
    unico = not isinstance(df.columns, pd.MultiIndex)
    for simb, (ativo, mercado) in simbolos.items():
        sub = df if unico else (df[simb] if simb in df.columns.get_level_values(0) else None)
        if sub is None or sub.empty:
            continue
        sub = sub.dropna(subset=["Close"])
        limite = ultimo_pregao(mercado_calendario(mercado), ctx.agora) if mercado != "FX" else ctx.agora.date()
        for data, r in sub.iterrows():
            d = pd.Timestamp(data).date()
            if d > limite:
                continue  # linha do "futuro" (fuso): descartada
            linhas.append({"ativo": ativo, "mercado": mercado, "data": d, "abertura": float(r["Open"]),
                           "maxima": float(r["High"]), "minima": float(r["Low"]), "fechamento": float(r["Close"]),
                           "ajustado": float(r.get("Adj Close", r["Close"])), "volume": float(r.get("Volume", 0) or 0),
                           "fonte": "yahoo"})
    return pd.DataFrame(linhas)


class Yahoo:
    nome = "yahoo"
    tabela = "precos"

    def __init__(self, download=None, incluir_benchmarks: bool = True):
        self._download = download
        self._bench = incluir_benchmarks

    def coletar(self, ctx: Contexto) -> tuple[pd.DataFrame, Coleta]:
        simbolos = {simbolo_yahoo(a, m): (a, m) for a, m in ctx.tickers}
        if self._bench:
            for nome, (simb, merc) in BENCHMARKS.items():
                simbolos[simb] = (nome, merc)
        if not simbolos:
            return pd.DataFrame(), Coleta(self.nome, True)
        inicio = (ctx.agora - timedelta(days=365 * ctx.anos_precos + 7)).date()
        fim = (ctx.agora + timedelta(days=1)).date()
        bruto = _baixar(list(simbolos), inicio, fim, self._download)
        df = normalizar(bruto, simbolos, ctx)
        faltando = sorted(set(simbolos.values()) - set(zip(df["ativo"], df["mercado"]))) if not df.empty else list(simbolos.values())
        return df, Coleta(self.nome, True, detalhe={"sem_dados": [a for a, _ in faltando]})


def cotacao_atual(tickers: list[tuple[str, str]]) -> dict[str, dict]:
    """Última cotação (atrasada ~15 min na B3) para a UI. Nunca lança: ativo sem dado fica de fora."""
    import yfinance as yf
    out = {}
    for ativo, mercado in tickers:
        try:
            fi = yf.Ticker(simbolo_yahoo(ativo, mercado)).fast_info
            out[ativo] = {"preco": float(fi["last_price"]), "fechamento_anterior": float(fi["previous_close"]),
                          "moeda": fi.get("currency")}
        except Exception:  # noqa: BLE001
            continue
    return out
