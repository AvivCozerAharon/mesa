from datetime import date

import pytest

from mesa import gatilhos as g
from mesa.armazenamento import Db

M = {"ultimo": 40.0, "mm50": 45.0, "mm200": 38.0, "vs_preco_medio_pct": -18.2, "retornos": {"1m": -3.0},
     "s52": {"pct_do_fundo": 0.4, "pct_do_topo": -30.0}, "vol_30d": 60.0, "vol_252d": 25.0, "drawdown": {"atual": -22.0},
     "janelas_12m": {"pct": 40.0}, "pct_do_bench": {"12m": 85.0}, "captacao_liquida_6m": -300.0, "pl": 1000.0}
NOT = [{"id": 1, "titulo": "Empresa X entra em recuperação judicial"}, {"id": 2, "titulo": "Lucro sobe"}]


def test_regras_individuais():
    assert g.queda_desde_compra(M, {"pct": 15}, [])["queda_pct"] == -18.2
    assert g.queda_desde_compra(M, {"pct": 20}, []) is None
    assert g.abaixo_mm50(M, {}, []) and g.abaixo_mm200(M, {}, []) is None
    assert g.min_52s(M, {}, []) and g.max_52s(M, {}, []) is None
    assert g.vol_spike(M, {}, [])["fator"] == 2 and g.vol_spike(M, {"fator": 3}, []) is None
    assert g.drawdown(M, {"pct": 20}, [])["drawdown_pct"] == -22.0
    assert g.noticia_contem(M, {"termos": ["recuperação judicial"]}, NOT)["noticias"][0]["id"] == 1
    assert g.noticia_contem(M, {"termos": ["fraude"]}, NOT) is None
    assert g.fundo_abaixo_bench(M, {}, []) and g.fundo_pct_bench_12m(M, {}, []) and g.fundo_resgate(M, {}, [])
    assert g.queda_desde_compra({}, {}, []) is None  # metrica ausente


@pytest.fixture
def db(tmp_path):
    d = Db(str(tmp_path / "m.db"))
    d.con.execute("INSERT INTO posicoes (id, ativo, tipo, mercado, identificador, quantidade, preco_medio, moeda, data_compra, ativa, criada_em, atualizada_em)"
                  " VALUES (1,'PETR4','acao','B3','PETR4',1,1,'BRL','2025-01-01',1,'t','t')")
    d.con.commit()
    return d


def test_padrao_idempotente_e_disparos(db):
    assert g.garantir_padrao(db, 1) == len(g.PADRAO) and g.garantir_padrao(db, 1) == 0
    assert len(g.listar(db, 1)) == len(g.PADRAO)
    novos = g.avaliar(db, 1, M, NOT, date(2026, 9, 22))
    regras = {n["regra"] for n in novos}
    assert regras == {"queda_desde_compra", "min_52s", "vol_spike", "noticia_contem", "fundo_abaixo_bench", "fundo_resgate"}
    assert any("15 %" in n["descricao"] for n in novos)
    assert g.avaliar(db, 1, M, NOT, date(2026, 9, 22)) == []  # mesmo estado: nada novo
    assert g.avaliar(db, 1, M, NOT, date(2026, 9, 23)) == []  # dia seguinte, mesmo estado: continua sem repetir
    # condicao sai e volta com outro valor -> dispara de novo
    m2 = {**M, "vs_preco_medio_pct": -2.0}
    assert not [n for n in g.avaliar(db, 1, m2, NOT, date(2026, 9, 24)) if n["regra"] == "queda_desde_compra"]
    m3 = {**M, "vs_preco_medio_pct": -25.0}
    assert [n for n in g.avaliar(db, 1, m3, NOT, date(2026, 9, 25)) if n["regra"] == "queda_desde_compra"]
    pend = g.pendentes(db)
    assert len(pend) == 7 and pend[0]["ativo"] == "PETR4"
    g.marcar_visto(db, pend[0]["id"])
    assert len(g.pendentes(db)) == 6


def test_criar_excluir_e_regra_invalida(db):
    gid = g.criar(db, 1, "drawdown", {"pct": 10})
    assert any(x["id"] == gid for x in g.listar(db, 1))
    g.excluir(db, gid)
    assert not any(x["id"] == gid for x in g.listar(db, 1))
    with pytest.raises(ValueError):
        g.criar(db, 1, "inexistente", {})
    gl = g.criar(db, None, "queda_1m", {"pct": 5})  # global: vale para toda posicao
    assert any(x["id"] == gl for x in g.listar(db, 1))
