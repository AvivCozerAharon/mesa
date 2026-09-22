"""Métricas de posição e de fundo. Funções puras sobre `pd.Series` indexadas por data (DatetimeIndex).

Convenções: retornos em %, vol anualizada por √252, CDI capitalizado por dia útil (a série do BCB
é % ao dia). Nenhuma função lê o relógio: `hoje` é sempre argumento.
"""
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

DIAS_ANO = 252


def _serie(s: pd.Series) -> pd.Series:
    s = s.dropna().astype(float)
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def _pct(a: float, b: float) -> float | None:
    return None if a is None or b is None or b == 0 else (a / b - 1) * 100


def retorno(serie: pd.Series, inicio: date, fim: date) -> float | None:
    s = _serie(serie)
    s = s[(s.index >= pd.Timestamp(inicio)) & (s.index <= pd.Timestamp(fim))]
    if len(s) < 2:
        return None
    return _pct(float(s.iloc[-1]), float(s.iloc[0]))


def retorno_periodos(serie: pd.Series, hoje: date, data_compra: date | None = None) -> dict:
    s = _serie(serie)
    if s.empty:
        return {}
    ultimo = float(s.iloc[-1])

    def desde(d: date) -> float | None:
        base = s[s.index <= pd.Timestamp(d)]
        return _pct(ultimo, float(base.iloc[-1])) if not base.empty else None

    out = {"1m": desde(hoje - timedelta(days=30)), "3m": desde(hoje - timedelta(days=91)),
           "6m": desde(hoje - timedelta(days=182)), "12m": desde(hoje - timedelta(days=365)),
           "ytd": desde(date(hoje.year - 1, 12, 31))}
    if data_compra:
        out["desde_compra"] = desde(data_compra)
    return out


def cdi_acumulado(cdi_diario: pd.Series, inicio: date, fim: date) -> float | None:
    """CDI em % ao dia (série SGS 12) capitalizado entre inicio (exclusivo) e fim (inclusivo), em %."""
    s = _serie(cdi_diario)
    s = s[(s.index > pd.Timestamp(inicio)) & (s.index <= pd.Timestamp(fim))]
    if s.empty:
        return None
    return (np.prod(1 + s.values / 100) - 1) * 100


def indice_cdi(cdi_diario: pd.Series) -> pd.Series:
    """Índice acumulado (base 100) para usar o CDI como 'preço' de benchmark."""
    s = _serie(cdi_diario)
    return 100 * (1 + s / 100).cumprod()


def drawdown(serie: pd.Series) -> dict:
    s = _serie(serie)
    if s.empty:
        return {}
    topo = s.cummax()
    dd = s / topo - 1
    maximo = float(dd.min()) * 100
    atual = float(dd.iloc[-1]) * 100
    # dias para recuperar o drawdown maximo (None se ainda nao recuperou)
    i_min = dd.idxmin()
    depois = s[s.index > i_min]
    recup = depois[depois >= topo.loc[i_min]]
    dias_rec = (recup.index[0] - i_min).days if not recup.empty else None
    em_dd_desde = None
    if atual < 0:
        em_dd_desde = s[s == topo.iloc[-1]].index[-1].date().isoformat()
    return {"maximo": maximo, "atual": atual, "dias_recuperacao_max": dias_rec, "em_drawdown_desde": em_dd_desde,
            "data_fundo_max": i_min.date().isoformat()}


def vol(serie: pd.Series, janela: int) -> float | None:
    s = _serie(serie)
    r = s.pct_change().dropna().tail(janela)
    if len(r) < max(5, janela // 2):
        return None
    return float(r.std(ddof=1) * math.sqrt(DIAS_ANO) * 100)


def media_movel(serie: pd.Series, n: int) -> float | None:
    s = _serie(serie)
    return float(s.tail(n).mean()) if len(s) >= n else None


def distancia_52s(serie: pd.Series, hoje: date) -> dict:
    s = _serie(serie)
    s = s[s.index >= pd.Timestamp(hoje - timedelta(days=365))]
    if s.empty:
        return {}
    topo, fundo, ultimo = float(s.max()), float(s.min()), float(s.iloc[-1])
    return {"topo": topo, "fundo": fundo, "pct_do_topo": _pct(ultimo, topo), "pct_do_fundo": _pct(ultimo, fundo),
            "data_topo": s.idxmax().date().isoformat(), "data_fundo": s.idxmin().date().isoformat()}


def _alinhar(a: pd.Series, b: pd.Series) -> pd.DataFrame:
    df = pd.concat([_serie(a).rename("a"), _serie(b).rename("b")], axis=1).dropna()
    return df


def beta(serie: pd.Series, bench: pd.Series, janela: int = DIAS_ANO) -> float | None:
    df = _alinhar(serie, bench).pct_change().dropna().tail(janela)
    if len(df) < 30 or df["b"].var() == 0:
        return None
    return float(df["a"].cov(df["b"]) / df["b"].var())


def janelas_moveis(serie: pd.Series, bench: pd.Series, meses: int = 12, passo_meses: int = 1) -> dict:
    """Quantas janelas de `meses` (deslizando de `passo_meses`) a série bateu o benchmark."""
    df = _alinhar(serie, bench)
    if df.empty:
        return {"n": 0, "batidas": 0, "pct": None}
    fim = df.index[-1]
    n = batidas = 0
    inicio_janela = fim - pd.DateOffset(months=meses)
    while inicio_janela >= df.index[0]:
        w = df[(df.index >= inicio_janela) & (df.index <= fim)]
        if len(w) >= 2:
            n += 1
            ra, rb = w["a"].iloc[-1] / w["a"].iloc[0], w["b"].iloc[-1] / w["b"].iloc[0]
            batidas += ra > rb
        fim = fim - pd.DateOffset(months=passo_meses)
        inicio_janela = fim - pd.DateOffset(months=meses)
    return {"n": n, "batidas": int(batidas), "pct": (batidas / n * 100) if n else None}


def contribuicao_risco(retornos: pd.DataFrame, pesos: pd.Series) -> pd.Series:
    """Contribuição percentual de cada ativo para a variância da carteira (soma 100)."""
    cols = [c for c in retornos.columns if c in pesos.index]
    r = retornos[cols].dropna()
    w = pesos[cols] / pesos[cols].sum()
    if r.empty or len(cols) == 0:
        return pd.Series(dtype=float)
    cov = r.cov().values
    var_p = float(w.values @ cov @ w.values)
    if var_p == 0:
        return pd.Series(0.0, index=cols)
    contrib = w.values * (cov @ w.values) / var_p * 100
    return pd.Series(contrib, index=cols)


def metricas_posicao(precos: pd.Series, bench: pd.Series | None, hoje: date, data_compra: date | None,
                     preco_medio: float | None) -> dict:
    s = _serie(precos)
    if s.empty:
        return {}
    ultimo = float(s.iloc[-1])
    out = {"ultimo": ultimo, "data_ultimo": s.index[-1].date().isoformat(),
           "retornos": retorno_periodos(s, hoje, data_compra), "drawdown": drawdown(s),
           "vol_30d": vol(s, 30), "vol_252d": vol(s, DIAS_ANO), "mm50": media_movel(s, 50), "mm200": media_movel(s, 200),
           "s52": distancia_52s(s, hoje)}
    if preco_medio:
        out["vs_preco_medio_pct"] = _pct(ultimo, preco_medio)
    if bench is not None and not _serie(bench).empty:
        b = _serie(bench)
        out["beta_12m"] = beta(s, b)
        rb = retorno_periodos(b, hoje, data_compra)
        out["bench_retornos"] = rb
        out["excesso"] = {k: (v - rb[k]) if v is not None and rb.get(k) is not None else None
                          for k, v in out["retornos"].items()}
    return out


def metricas_fundo(cota: pd.Series, bench: pd.Series | None, pares: pd.DataFrame | None, hoje: date,
                   taxa_adm: float | None, taxa_perf: float | None, pl: pd.Series | None,
                   captacao_liquida: pd.Series | None, data_compra: date | None = None) -> dict:
    """`pares`: DataFrame (index=data, colunas=cnpj) com cotas dos fundos da mesma classe."""
    s = _serie(cota)
    if s.empty:
        return {}
    out = {"ultimo": float(s.iloc[-1]), "data_ultimo": s.index[-1].date().isoformat(),
           "retornos": retorno_periodos(s, hoje, data_compra), "drawdown": drawdown(s), "vol_252d": vol(s, DIAS_ANO),
           "taxa_adm": taxa_adm, "taxa_perf": taxa_perf}
    if bench is not None and not _serie(bench).empty:
        b = _serie(bench)
        rb = retorno_periodos(b, hoje, data_compra)
        out["bench_retornos"] = rb
        out["pct_do_bench"] = {k: (v / rb[k] * 100) if v is not None and rb.get(k) not in (None, 0) else None
                               for k, v in out["retornos"].items()}
        out["janelas_12m"] = janelas_moveis(s, b, 12)
    if pares is not None and not pares.empty:
        rets = {}
        for k, dias in (("12m", 365), ("36m", 3 * 365)):
            base = pares[pares.index <= pd.Timestamp(hoje - timedelta(days=dias))]
            if base.empty:
                continue
            r = (pares.iloc[-1] / base.iloc[-1] - 1) * 100
            r = r.dropna()
            proprio = out["retornos"].get(k)
            rets[k] = {"mediana_pares": float(r.median()) if not r.empty else None, "n_pares": int(len(r)),
                       "percentil": float((r < proprio).mean() * 100) if proprio is not None and not r.empty else None}
        out["pares"] = rets
    if pl is not None and not _serie(pl).empty:
        p = _serie(pl)
        out["pl"] = float(p.iloc[-1])
        base = p[p.index <= pd.Timestamp(hoje - timedelta(days=182))]
        out["pl_var_6m_pct"] = _pct(float(p.iloc[-1]), float(base.iloc[-1])) if not base.empty else None
        if taxa_adm is not None:
            out["custo_anual_estimado"] = float(p.tail(DIAS_ANO).mean()) * taxa_adm / 100
    if captacao_liquida is not None and not _serie(captacao_liquida).empty:
        c = _serie(captacao_liquida)
        out["captacao_liquida_6m"] = float(c[c.index >= pd.Timestamp(hoje - timedelta(days=182))].sum())
    return out
