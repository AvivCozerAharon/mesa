from datetime import date, datetime, timezone

import pandas as pd

from mesa.armazenamento import Consulta, Db, gravar_parquet


def _precos(valor, coletado):
    return pd.DataFrame({"ativo": ["PETR4"], "mercado": ["B3"], "data": [date(2026, 9, 18)], "abertura": [1.0],
                         "maxima": [1.0], "minima": [1.0], "fechamento": [valor], "ajustado": [valor], "volume": [10],
                         "fonte": ["yahoo"], "coletado_em": [coletado]})


def test_coleta_mais_recente_vence(tmp_path):
    gravar_parquet(tmp_path, "precos", date(2026, 9, 18), _precos(48.0, datetime(2026, 9, 18, 21, tzinfo=timezone.utc)))
    gravar_parquet(tmp_path, "precos", date(2026, 9, 19), _precos(48.5, datetime(2026, 9, 19, 21, tzinfo=timezone.utc)))
    df = Consulta(tmp_path).precos(["PETR4"])
    assert len(df) == 1 and df["fechamento"].iloc[0] == 48.5


def test_regravar_mesmo_dia_sobrescreve(tmp_path):
    p1 = gravar_parquet(tmp_path, "precos", date(2026, 9, 18), _precos(1.0, datetime(2026, 9, 18, tzinfo=timezone.utc)))
    p2 = gravar_parquet(tmp_path, "precos", date(2026, 9, 18), _precos(2.0, datetime(2026, 9, 18, 1, tzinfo=timezone.utc)))
    assert p1 == p2 and Consulta(tmp_path).precos(["PETR4"])["fechamento"].iloc[0] == 2.0


def test_pasta_vazia_nao_quebra(tmp_path):
    c = Consulta(tmp_path)
    assert c.precos(["PETR4"]).empty and c.benchmark("CDI").empty and c.cotas(["1"]).empty
    assert c.ultima_data("precos", "ativo", "PETR4") is None


def test_benchmark_serie_e_ultima_data(tmp_path):
    df = pd.DataFrame({"nome": ["CDI", "CDI"], "data": [date(2026, 9, 17), date(2026, 9, 18)], "valor": [0.05, 0.051]})
    gravar_parquet(tmp_path, "benchmarks", date(2026, 9, 19), df)
    s = Consulta(tmp_path).benchmark("CDI")
    assert list(s.values) == [0.05, 0.051] and s.index[0] == pd.Timestamp("2026-09-17")
    assert Consulta(tmp_path).ultima_data("benchmarks", "nome", "CDI") == date(2026, 9, 18)


def test_db_coletas_e_metricas(tmp_path):
    db = Db(str(tmp_path / "m.db"))
    db.registrar_coleta("yahoo", date(2026, 9, 18), 120, 900)
    db.registrar_coleta("cvm", date(2026, 9, 18), None, 30, erro="timeout")
    ult = db.ultimas_coletas()
    assert ult[0]["fonte"] == "cvm" and ult[0]["erro"] == "timeout" and ult[1]["linhas"] == 120
    db.gravar_metricas(date(2026, 9, 17), 1, {"p": 1})
    db.gravar_metricas(date(2026, 9, 18), 1, {"p": 2})
    db.gravar_metricas(date(2026, 9, 18), 2, {"p": 3})
    rec = db.metricas_recentes()
    assert rec[1]["p"] == 2 and rec[2]["p"] == 3 and rec[1]["data_metricas"] == "2026-09-18"
