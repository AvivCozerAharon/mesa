from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from mesa import gatilhos as g
from mesa import ia
from mesa.fontes import Contexto, executar
from mesa.fontes.fundamentos import Fundamentos, extrair

BRT = ZoneInfo("America/Sao_Paulo")
INFO = {"trailingPE": 4.73, "priceToBook": 1.29, "enterpriseToEbitda": 3.84, "ebitda": 243e9, "totalRevenue": 548e9,
        "ebitdaMargins": 0.443, "profitMargins": 0.243, "returnOnEquity": 0.303, "totalDebt": 366e9, "totalCash": 54e9,
        "dividendYield": 8.94, "payoutRatio": 0.287, "revenueGrowth": 0.42, "financialCurrency": "BRL"}
DRE = pd.DataFrame({pd.Timestamp("2026-06-30"): [140e9, 60e9, 30e9], pd.Timestamp("2026-03-31"): [150e9, 62e9, 31e9],
                    pd.Timestamp("2025-12-31"): [160e9, 65e9, 33e9]}, index=["Total Revenue", "EBITDA", "Net Income"])


class FakeTicker:
    def __init__(self, simbolo):
        self.simbolo = simbolo
        if simbolo == "QUEBRA.SA":
            raise RuntimeError("sem dados")
        self.info = INFO if simbolo == "PETR4.SA" else {}
        self.quarterly_income_stmt = DRE if simbolo == "PETR4.SA" else pd.DataFrame()


def test_extrair_normaliza_unidades_e_trimestres():
    f, tri = extrair(INFO, DRE, "PETR4", "B3", date(2026, 9, 22))
    assert f["margem_ebitda"] == 44.3 and f["dy"] == 8.94 and round(f["divida_liq_ebitda"], 2) == 1.28
    assert f["ultimo_balanco"] == date(2026, 6, 30) and len(tri) == 3 and tri[0]["receita"] == 140e9
    assert f["moeda_dre"] == "BRL"  # 3 trimestres so: nao da para inferir
    dre4 = DRE.copy(); dre4[pd.Timestamp("2025-09-30")] = [90e9, 40e9, 20e9]
    f3, _ = extrair(INFO, dre4, "PETR4", "B3", date(2026, 9, 22))  # 548 bi / 540 bi = mesma moeda
    assert f3["moeda_dre"] == "BRL"
    f4, _ = extrair({**INFO, "totalRevenue": 3000e9}, dre4, "PETR4", "B3", date(2026, 9, 22))  # razao ~5.6 -> DRE em USD
    assert f4["moeda_dre"] == "USD"
    f2, _ = extrair({"dividendYield": 0.32}, None, "AAPL", "US", date(2026, 9, 22))
    assert f2["dy"] == 0.32 and f2["divida_liq_ebitda"] is None  # ja vem em %; nao multiplicar


def test_adapter_filtra_tipos_e_isola_erros(tmp_path):
    ctx = Contexto(agora=datetime(2026, 9, 22, 7, tzinfo=BRT), tickers=[("PETR4", "B3"), ("VOO", "US"), ("QUEBRA", "B3"), ("XPTO", "US")],
                   cnpjs=[], dados_dir=str(tmp_path))
    fonte = Fundamentos({"PETR4": "acao", "VOO": "etf_us", "QUEBRA": "acao", "XPTO": "acao_us"}, ticker_factory=FakeTicker)
    df, col = executar(fonte, ctx)
    assert col.ok and list(df["ativo"]) == ["PETR4"] and len(fonte.dre) == 3
    assert "QUEBRA" in col.erro and "XPTO" in col.erro and col.detalhe["ativos"] == 1


def test_gatilhos_de_fundamentos():
    m = {"fundamentos": {"margem_ebitda": 8.0, "divida_liq_ebitda": 3.5},
         "trimestres": [{"trimestre": "2025-12-31", "receita": 160e9}, {"trimestre": "2026-03-31", "receita": 150e9}, {"trimestre": "2026-06-30", "receita": 140e9}]}
    assert g.receita_caiu_2tri(m, {}, [])["trimestre"] == "2026-06-30"
    assert g.margem_ebitda_abaixo(m, {}, [])["limite"] == 10 and g.margem_ebitda_abaixo(m, {"pct": 5}, []) is None
    assert g.divida_liq_ebitda_acima(m, {}, []) and g.descrever("divida_liq_ebitda_acima", {}) == "dívida líquida acima de 3× o EBITDA"
    assert g.receita_caiu_2tri({"trimestres": []}, {}, []) is None


def test_entrada_da_ia_inclui_fundamentos_e_validador_aceita_leitura():
    f, tri = extrair(INFO, DRE, "PETR4", "B3", date(2026, 9, 22))
    m = {"ultimo": 48.0, "retornos": {"12m": 69.5}, "fundamentos": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in f.items()},
         "trimestres": [{**t, "trimestre": t["trimestre"].isoformat()} for t in tri]}
    e = ia.montar_entrada({"id": 1, "ativo": "PETR4", "tipo": "acao", "preco_medio": 32.1, "data_compra": "2025-02-14"}, "dividendos", m, [], [], date(2026, 9, 22))
    assert e["fatos"]["fundamentos"]["dy"] == 8.9 and e["fatos"]["fundamentos"]["receita_bi"] == 548.0 and len(e["fatos"]["trimestres_bi"]["valores"]) == 3
    saida = {"situacao": "PETR4 a 48.0.", "leitura_fundamentos": "Margem EBITDA de 44.3% e dívida líquida de 1.3x o EBITDA; DY 8.9% com payout 28.7%.",
             "tese_continua": "nao_avaliavel", "justificativa": "Sem notícias.", "citacoes": [], "pontos_de_atencao": []}
    assert ia.validar(e, saida) == (True, [])
    ok, p = ia.validar(e, {**saida, "leitura_fundamentos": "Está barata: P/L de 3.1."})
    assert not ok and any("3.1" in x for x in p)
