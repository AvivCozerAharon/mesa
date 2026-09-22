"""Fundamentos de ações (B3 e EUA) via Yahoo Finance: múltiplos e últimos trimestres de DRE.

Mesma ressalva do adapter de preços: fonte não oficial, atrás de interface própria, com o histórico
guardado em Parquet. Números vêm com defasagem (balanço do trimestre anterior) e, na B3, às vezes com
erro de unidade — por isso a tela mostra a data do último balanço e a IA recebe só o que está aqui.
Fonte oficial para depois: CVM DFP/ITR (B3) e SEC companyfacts (EUA).
"""
import pandas as pd

from mesa.fontes import Coleta, Contexto
from mesa.fontes.yahoo import simbolo_yahoo

CAMPOS = {"pe": "trailingPE", "pe_projetado": "forwardPE", "pvp": "priceToBook", "ev_ebitda": "enterpriseToEbitda",
          "ebitda": "ebitda", "receita": "totalRevenue", "margem_ebitda": "ebitdaMargins", "margem_liquida": "profitMargins",
          "roe": "returnOnEquity", "divida_total": "totalDebt", "caixa": "totalCash", "fcf": "freeCashflow",
          "dy": "dividendYield", "payout": "payoutRatio", "cresc_receita": "revenueGrowth", "cresc_lucro": "earningsGrowth",
          "market_cap": "marketCap", "beta": "beta", "moeda": "financialCurrency"}
LINHAS_DRE = {"receita": "Total Revenue", "ebitda": "EBITDA", "lucro_operacional": "Operating Income", "lucro": "Net Income"}
TIPOS_COM_FUNDAMENTOS = {"acao", "acao_us", "bdr"}


def _f(x):
    try:
        v = float(x)
        return None if v != v else v  # NaN
    except (TypeError, ValueError):
        return None


def extrair(info: dict, dre: pd.DataFrame | None, ativo: str, mercado: str, data_coleta) -> tuple[dict, list[dict]]:
    f = {"ativo": ativo, "mercado": mercado, "data": data_coleta}
    for k, chave in CAMPOS.items():
        f[k] = info.get(chave) if k == "moeda" else _f(info.get(chave))
    # yfinance entrega dividendYield ja em % (AAPL 0.32, PETR4 8.94). Um "if < 1: *100" aqui transformou
    # 0.32 % em 32 % e a IA repetiu o numero com confianca: o validador cobra coerencia com a entrada, nao verdade.
    for k in ("margem_ebitda", "margem_liquida", "roe", "payout", "cresc_receita", "cresc_lucro"):
        if f[k] is not None:
            f[k] *= 100
    if f["ebitda"] and f["divida_total"] is not None:
        f["divida_liq_ebitda"] = ((f["divida_total"] or 0) - (f["caixa"] or 0)) / f["ebitda"]
    else:
        f["divida_liq_ebitda"] = None
    trimestres = []
    if dre is not None and not dre.empty:
        for col in list(dre.columns)[:8]:
            t = {"ativo": ativo, "trimestre": pd.Timestamp(col).date(), "data": data_coleta}
            for k, linha in LINHAS_DRE.items():
                t[k] = _f(dre.loc[linha, col]) if linha in dre.index else None
            trimestres.append(t)
        if trimestres:
            f["ultimo_balanco"] = max(t["trimestre"] for t in trimestres)
    f.setdefault("ultimo_balanco", None)
    # Yahoo: para tickers da B3 o `info` vem em BRL e as demonstracoes em USD (Petrobras: 548 bi vs 89 bi/ano).
    # Inferimos pela razao receita_12m / soma dos 4 ultimos trimestres: perto de 1 = mesma moeda; ~5-6 = USD.
    f["moeda_dre"] = f["moeda"]
    ttm = [t["receita"] for t in trimestres[:4] if t.get("receita")]
    if f.get("receita") and len(ttm) == 4 and sum(ttm) > 0:
        razao = f["receita"] / sum(ttm)
        if 3 < razao < 8 and mercado == "B3":
            f["moeda_dre"] = "USD"
    return f, [t for t in trimestres if t.get("receita")]


class Fundamentos:
    nome = "fundamentos"
    tabela = "fundamentos"
    tabela_secundaria = "dre_trimestral"

    def __init__(self, tipos_por_ativo: dict[str, str] | None = None, ticker_factory=None):
        self._tipos = tipos_por_ativo or {}
        self._factory = ticker_factory  # (simbolo) -> objeto com .info e .quarterly_income_stmt

    def coletar(self, ctx: Contexto) -> tuple[pd.DataFrame, Coleta]:
        factory = self._factory
        if factory is None:
            import yfinance as yf
            factory = yf.Ticker
        linhas, dres, erros = [], [], []
        for ativo, mercado in ctx.tickers:
            if self._tipos.get(ativo, "acao") not in TIPOS_COM_FUNDAMENTOS:
                continue
            try:
                t = factory(simbolo_yahoo(ativo, mercado))
                info = t.info or {}
                dre = t.quarterly_income_stmt
            except Exception as e:  # noqa: BLE001 - um ticker sem dados nao derruba os outros
                erros.append(f"{ativo}: {type(e).__name__}")
                continue
            f, tri = extrair(info, dre, ativo, mercado, ctx.agora.date())
            if f.get("receita") is None and f.get("pe") is None and not tri:
                erros.append(f"{ativo}: sem fundamentos")
                continue
            linhas.append(f)
            dres += tri
        self.dre = pd.DataFrame(dres)
        df = pd.DataFrame(linhas)
        return df, Coleta(self.nome, True, erro="; ".join(erros) or None, detalhe={"ativos": len(linhas), "trimestres": len(dres)})
