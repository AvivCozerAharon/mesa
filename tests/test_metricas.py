from datetime import date

import numpy as np
import pandas as pd
import pytest

from mesa.metricas import (beta, cdi_acumulado, contribuicao_risco, distancia_52s, drawdown, janelas_moveis,
                           metricas_fundo, metricas_posicao, retorno, retorno_periodos, vol)


def serie(valores, inicio="2026-01-02"):
    idx = pd.bdate_range(inicio, periods=len(valores))
    return pd.Series(valores, index=idx)


def test_retorno_e_periodos():
    s = serie([100 * 1.01 ** i for i in range(11)])
    assert retorno(s, s.index[0].date(), s.index[-1].date()) == pytest.approx(10.46, abs=0.01)
    r = retorno_periodos(s, s.index[-1].date(), data_compra=s.index[5].date())
    assert r["desde_compra"] == pytest.approx(5.10, abs=0.01)
    assert retorno(s, date(2030, 1, 1), date(2030, 2, 1)) is None


def test_cdi_acumulado_capitaliza():
    cdi = serie([0.05] * 21)
    assert cdi_acumulado(cdi, cdi.index[0].date() - pd.Timedelta(days=1), cdi.index[-1].date()) == pytest.approx(1.0555, abs=0.001)


def test_drawdown():
    d = drawdown(serie([100, 120, 90, 100, 130]))
    assert d["maximo"] == pytest.approx(-25) and d["atual"] == 0 and d["dias_recuperacao_max"] == 2
    d2 = drawdown(serie([100, 120, 90]))
    assert d2["atual"] == pytest.approx(-25) and d2["dias_recuperacao_max"] is None and d2["em_drawdown_desde"] == "2026-01-05"


def test_vol_e_52s():
    rng = np.random.default_rng(1)
    s = serie(100 * np.cumprod(1 + rng.normal(0, 0.01, 300)))
    v = vol(s, 252)
    assert 12 < v < 20  # ~1% ao dia -> ~16% aa
    assert vol(serie([1, 2, 3]), 30) is None
    d = distancia_52s(s, s.index[-1].date())
    assert d["pct_do_topo"] <= 0 <= d["pct_do_fundo"]


def test_beta_e_janelas():
    b = serie(100 * np.cumprod(1 + np.random.default_rng(2).normal(0, 0.01, 300)))
    a = 100 * (b / 100) ** 2  # retornos ~2x
    assert beta(a, b) == pytest.approx(2, abs=0.15)
    j = janelas_moveis(a, b, meses=3)
    assert j["n"] >= 8 and j["pct"] is not None
    plano = serie([100] * 300)
    assert janelas_moveis(plano, b, meses=3)["batidas"] < janelas_moveis(plano, b, meses=3)["n"]


def test_contribuicao_risco_soma_100():
    rng = np.random.default_rng(3)
    r = pd.DataFrame({"A": rng.normal(0, 0.02, 200), "B": rng.normal(0, 0.005, 200)})
    c = contribuicao_risco(r, pd.Series({"A": 0.5, "B": 0.5}))
    assert c.sum() == pytest.approx(100) and c["A"] > c["B"]


def test_metricas_posicao_e_fundo():
    s = serie([100 * 1.001 ** i for i in range(400)])
    b = serie([100 * 1.0005 ** i for i in range(400)])
    m = metricas_posicao(s, b, s.index[-1].date(), s.index[100].date(), 105.0)
    assert m["mm200"] is not None and m["excesso"]["12m"] > 0 and m["vs_preco_medio_pct"] > 0
    pares = pd.DataFrame({"p1": b.values, "p2": (b * 1.5).values}, index=b.index)
    pl = serie([1e6] * 400)
    f = metricas_fundo(s, b, pares, s.index[-1].date(), taxa_adm=2.0, taxa_perf=20.0, pl=pl, captacao_liquida=serie([10] * 400))
    n = len(s[s.index > s.index[-1] - pd.Timedelta(days=365)]) - 1  # dias uteis na janela de 12 m
    esperado = (1.001 ** n - 1) / (1.0005 ** n - 1) * 100
    assert f["pct_do_bench"]["12m"] == pytest.approx(esperado, abs=2)
    assert f["janelas_12m"]["pct"] == 100 and f["pares"]["12m"]["n_pares"] == 2
    assert f["custo_anual_estimado"] == pytest.approx(20000) and f["captacao_liquida_6m"] > 0
