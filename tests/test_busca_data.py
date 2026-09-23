import csv
import io
import zipfile
from datetime import date

import pandas as pd
import pytest

from mesa import busca
from mesa.armazenamento import Consulta, gravar_parquet

CNPJ = "22215116000180"


def zip_informe(linhas) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["CNPJ_FUNDO_CLASSE", "DT_COMPTC", "VL_QUOTA", "VL_PATRIM_LIQ", "CAPTC_DIA", "RESG_DIA", "NR_COTST"])
    for d, cota in linhas:
        w.writerow([CNPJ, d, cota, "932000000", "0", "0", "100"])
    saida = io.BytesIO()
    with zipfile.ZipFile(saida, "w") as zf:
        zf.writestr("inf_diario_fi_202608.csv", buf.getvalue().encode("latin-1"))
    return saida.getvalue()


@pytest.fixture
def consulta(tmp_path):
    gravar_parquet(str(tmp_path), "curvas", date(2026, 9, 22), pd.DataFrame([
        {"pais": "BR", "data": date(2025, 3, 3), "titulo": "Tesouro IPCA+", "vencimento": date(2029, 5, 15), "taxa": 7.2, "pu": 3244.93, "prazo_anos": 4.2},
        {"pais": "BR", "data": date(2026, 9, 18), "titulo": "Tesouro IPCA+", "vencimento": date(2029, 5, 15), "taxa": 7.6, "pu": 3913.82, "prazo_anos": 2.6},
    ]), fonte="tesouro")
    gravar_parquet(str(tmp_path), "precos", date(2026, 9, 22), pd.DataFrame([
        {"ativo": "PETR4", "data": date(2025, 2, 13), "abertura": 31.0, "maxima": 32.5, "minima": 30.9, "fechamento": 32.10, "ajustado": 32.10, "volume": 1e6},
        {"ativo": "PETR4", "data": date(2026, 9, 18), "abertura": 47.0, "maxima": 48.5, "minima": 46.9, "fechamento": 48.00, "ajustado": 48.00, "volume": 1e6},
    ]), fonte="yahoo")
    return Consulta(str(tmp_path))


def test_preco_em_usa_o_ultimo_pregao_ate_a_data(consulta):
    r = busca.preco_em(consulta, "tesouro", "Tesouro IPCA+ 2029", date(2025, 3, 5))
    assert r["preco"] == pytest.approx(3244.93) and r["data"] == "2025-03-03"
    r = busca.preco_em(consulta, "acao", "PETR4", date(2025, 2, 14))  # dia sem pregao na base: cai no anterior
    assert r["preco"] == pytest.approx(32.10) and r["data"] == "2025-02-13" and r["fonte"] == "fechamento guardado"
    assert busca.preco_em(consulta, "tesouro", "Tesouro IPCA+ 2029", date(2020, 1, 2))["erro"]


def test_preco_em_ticker_novo_vai_ao_yahoo(consulta):
    chamado = {}

    def historico(ativo, mercado, quando):
        chamado.update(ativo=ativo, mercado=mercado, quando=quando)
        return {"preco": 190.0, "data": "2025-06-02", "fonte": "fechamento do Yahoo"}

    r = busca.preco_em(consulta, "acao_us", "AAPL", date(2025, 6, 2), historico_fn=historico)
    assert r["preco"] == 190.0 and chamado["mercado"] == "US"
    assert busca.preco_em(consulta, "acao_us", "XPTO", date(2025, 6, 2), historico_fn=lambda *a: None)["erro"]


def test_cota_cvm_le_o_zip_em_cache_e_baixa_o_que_falta(tmp_path, consulta):
    import os
    os.makedirs(tmp_path / "cvm" / "raw", exist_ok=True)
    (tmp_path / "cvm" / "raw" / "inf_diario_fi_202608.zip").write_bytes(zip_informe([("2026-08-03", "3.01"), ("2026-08-04", "3.05")]))
    r = busca.cota_cvm(str(tmp_path), "22.215.116/0001-80", date(2026, 8, 5))
    assert r["preco"] == pytest.approx(3.05) and r["data"] == "2026-08-04" and r["fonte"] == "informe diário da CVM"

    baixados = []

    class R:
        content = zip_informe([("2026-07-15", "2.90")])

        def raise_for_status(self):
            pass

    def get(url):
        baixados.append(url)
        return R()

    r = busca.cota_cvm(str(tmp_path), CNPJ, date(2026, 7, 20), get=get)
    assert r["preco"] == pytest.approx(2.90) and len(baixados) == 1 and "202607" in baixados[0]


def test_preco_em_fundo_prefere_a_serie_ja_coletada(tmp_path):
    gravar_parquet(str(tmp_path), "cotas_fundos", date(2026, 9, 22), pd.DataFrame([
        {"cnpj": CNPJ, "data": date(2026, 8, 4), "cota": 3.05, "pl": 932e6, "captacao": 0.0, "resgate": 0.0, "cotistas": 100},
    ]))
    r = busca.preco_em(Consulta(str(tmp_path)), "fundo", CNPJ, date(2026, 8, 6), dados_dir=str(tmp_path))
    assert r == {"preco": pytest.approx(3.05), "data": "2026-08-04", "fonte": "CVM"}
