from mesa.evals import rodar_casamento, rodar_validacao


def test_casamento_bate_os_casos_rotulados():
    r = rodar_casamento()
    assert r["n"] == 30 and r["precisao"] >= 0.9 and r["recall"] >= 0.9, r["erros"]


def test_validador_bate_os_casos():
    r = rodar_validacao()
    assert r["acertos"] == r["n"], r["erros"]
