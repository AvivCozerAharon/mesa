import csv
import io
import zipfile
from datetime import date

import pandas as pd
import pytest

from mesa import busca
from mesa.armazenamento import Consulta, gravar_parquet, partes_fundo
from mesa.carteira import Posicao
from mesa.fontes.cvm import parse_informe

CNPJ = "00888897000131"
SUB_A, SUB_B = "RBMFN1747320951", "MZMRC1747322915"


def informe_csv(linhas) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["TP_FUNDO_CLASSE", "CNPJ_FUNDO_CLASSE", "ID_SUBCLASSE", "DT_COMPTC", "VL_TOTAL", "VL_QUOTA",
                "VL_PATRIM_LIQ", "CAPTC_DIA", "RESG_DIA", "NR_COTST"])
    for d, sub, cota in linhas:
        w.writerow(["CLASSES - FIF", "00.888.897/0001-31", sub, d, "1", cota, "100", "0", "0", "1"])
    return buf.getvalue().encode("latin-1")


def test_parse_informe_traz_a_subclasse():
    df = parse_informe(informe_csv([("2026-09-01", SUB_A, "73.28"), ("2026-09-01", SUB_B, "72.96"),
                                    ("2026-09-01", "", "50.00")]), {CNPJ})
    assert list(df["subclasse"]) == [SUB_A, SUB_B, ""] and len(df) == 3


def test_identificador_com_subclasse_valida_e_normaliza():
    p = Posicao("", "fundo", "00.888.897/0001-31:rbmfn1747320951", 10, 73.0, date(2026, 9, 1))
    assert p.identificador == f"{CNPJ}:{SUB_A}" and partes_fundo(p.identificador) == (CNPJ, SUB_A)
    assert Posicao("", "fundo", "00.888.897/0001-31", 10, 73.0, date(2026, 9, 1)).identificador == CNPJ
    with pytest.raises(ValueError, match="CNPJ"):
        Posicao("", "fundo", "123:ABC", 1, 1, date(2026, 9, 1))


@pytest.fixture
def consulta(tmp_path):
    gravar_parquet(str(tmp_path), "cotas_fundos", date(2026, 9, 22), pd.DataFrame([
        {"cnpj": CNPJ, "subclasse": SUB_A, "data": date(2026, 9, 1), "cota": 73.28, "pl": 1e8, "captacao": 0.0, "resgate": 0.0, "cotistas": 5},
        {"cnpj": CNPJ, "subclasse": SUB_A, "data": date(2026, 9, 2), "cota": 73.33, "pl": 1e8, "captacao": 0.0, "resgate": 0.0, "cotistas": 5},
        {"cnpj": CNPJ, "subclasse": SUB_B, "data": date(2026, 9, 2), "cota": 72.97, "pl": 9e7, "captacao": 0.0, "resgate": 0.0, "cotistas": 3},
        {"cnpj": "22215116000180", "subclasse": "", "data": date(2026, 9, 2), "cota": 3.12, "pl": 9e8, "captacao": 0.0, "resgate": 0.0, "cotistas": 9},
    ]))
    return Consulta(str(tmp_path))


def test_cotas_separa_as_subclasses(consulta):
    df = consulta.cotas([f"{CNPJ}:{SUB_A}"])
    assert list(df["cota"]) == [73.28, 73.33] and set(df["identificador"]) == {f"{CNPJ}:{SUB_A}"}
    assert consulta.cotas([CNPJ]).empty  # esta classe so publica por subclasse
    assert list(consulta.cotas(["22215116000180"])["identificador"]) == ["22215116000180"]
    # para os pares, uma serie por CNPJ basta
    pares = consulta.cotas([CNPJ], por_cnpj=True)
    assert len(pares) == 2 and pares["data"].nunique() == 2


def test_preco_em_e_cotacao_da_subclasse(consulta, tmp_path):
    r = busca.cotacao(consulta, "fundo", f"{CNPJ}:{SUB_B}")
    assert r["preco"] == pytest.approx(72.97)
    r = busca.preco_em(consulta, "fundo", f"{CNPJ}:{SUB_A}", date(2026, 9, 1), dados_dir=str(tmp_path))
    assert r["preco"] == pytest.approx(73.28) and r["data"] == "2026-09-01"


def test_cota_cvm_filtra_a_subclasse_no_zip(tmp_path):
    import os
    os.makedirs(tmp_path / "cvm" / "raw", exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("inf.csv", informe_csv([("2026-09-01", SUB_A, "73.28"), ("2026-09-01", SUB_B, "72.96")]))
    (tmp_path / "cvm" / "raw" / "inf_diario_fi_202609.zip").write_bytes(buf.getvalue())
    r = busca.cota_cvm(str(tmp_path), f"{CNPJ}:{SUB_B}", date(2026, 9, 3))
    assert r["preco"] == pytest.approx(72.96)


def test_busca_mostra_o_identificador(tmp_path):
    gravar_parquet(str(tmp_path), "fundos_cadastro", date(2026, 9, 22), pd.DataFrame([
        {"cnpj": CNPJ, "subclasse": "", "nome": "ALASKA BLACK FIC FIA", "gestor": "ALASKA", "classe": "Ações", "pl": 2e9, "situacao": "EM FUNCIONAMENTO NORMAL"},
        {"cnpj": CNPJ, "subclasse": SUB_A, "nome": "ALASKA BLACK FIC FIA - CLASSE A", "gestor": "ALASKA", "classe": "Ações", "pl": 2e9, "situacao": "EM FUNCIONAMENTO NORMAL"},
    ]))
    r = busca.fundos(Consulta(str(tmp_path)), "alaska black")
    por_id = {x["identificador"]: x for x in r}
    assert set(por_id) == {CNPJ, f"{CNPJ}:{SUB_A}"}
    assert por_id[CNPJ]["codigo"] == "00.888.897/0001-31"
    assert por_id[f"{CNPJ}:{SUB_A}"]["codigo"] == f"00.888.897/0001-31 · {SUB_A}"
    assert por_id[f"{CNPJ}:{SUB_A}"]["detalhe"].startswith("subclasse")
    assert [x["identificador"] for x in busca.fundos(Consulta(str(tmp_path)), "00.888.897/0001-31")] == [CNPJ, f"{CNPJ}:{SUB_A}"]
