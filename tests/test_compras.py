from datetime import date

import pytest

from mesa.armazenamento import Db
from mesa.carteira import Carteira, Posicao, somar_compras


@pytest.fixture
def carteira(tmp_path):
    return Carteira(Db(str(tmp_path / "m.db")))


def nova(c, **kw):
    p = Posicao("", "acao", "PETR4", kw.pop("quantidade", 100), kw.pop("preco_medio", 32.10), kw.pop("data_compra", date(2025, 2, 14)))
    return c.criar(p, **kw)


def test_posicao_criada_vira_uma_compra(carteira):
    pid = nova(carteira)
    cs = carteira.compras(pid)
    assert len(cs) == 1 and cs[0]["quantidade"] == 100 and cs[0]["preco"] == 32.10 and cs[0]["data"] == "2025-02-14"


def test_aporte_recalcula_preco_medio_ponderado(carteira):
    pid = nova(carteira)
    p = carteira.adicionar_compra(pid, date(2026, 3, 10), 50, 45.0)
    assert p.quantidade == 150
    assert p.preco_medio == pytest.approx((100 * 32.10 + 50 * 45.0) / 150)
    assert p.data_compra == date(2025, 2, 14)  # a posicao guarda a data da primeira compra
    # compra anterior a primeira puxa a data da posicao para tras
    p = carteira.adicionar_compra(pid, date(2024, 12, 2), 10, 28.0)
    assert p.data_compra == date(2024, 12, 2) and p.quantidade == 160
    assert [c["data"] for c in carteira.compras(pid)] == ["2024-12-02", "2025-02-14", "2026-03-10"]


def test_remover_compra_e_a_ultima_protegida(carteira):
    pid = nova(carteira)
    carteira.adicionar_compra(pid, date(2026, 3, 10), 50, 45.0)
    cid = carteira.compras(pid)[-1]["id"]
    p = carteira.remover_compra(cid)
    assert p.quantidade == 100 and p.preco_medio == pytest.approx(32.10)
    with pytest.raises(ValueError, match="única compra"):
        carteira.remover_compra(carteira.compras(pid)[0]["id"])
    with pytest.raises(KeyError):
        carteira.remover_compra(9999)


def test_compra_invalida_e_posicao_inexistente(carteira):
    pid = nova(carteira)
    with pytest.raises(ValueError):
        carteira.adicionar_compra(pid, date(2026, 3, 10), 0, 45.0)
    with pytest.raises(ValueError):
        carteira.adicionar_compra(pid, date(2026, 3, 10), 5, -1)
    with pytest.raises(KeyError):
        carteira.adicionar_compra(777, date(2026, 3, 10), 5, 1)
    with pytest.raises(ValueError, match="somar mais que zero"):
        somar_compras([])


def test_criar_com_varias_compras_de_uma_vez(carteira):
    compras = [{"data": "2025-05-20", "quantidade": 50, "preco": 58.40},
               {"data": date(2026, 1, 2), "quantidade": 30, "preco": 70.00}]
    q, pm, primeira = somar_compras(compras)
    pid = carteira.criar(Posicao("", "acao", "VALE3", q, pm, primeira), None, compras)
    pos = carteira.obter(pid)
    assert pos.quantidade == 80 and pos.preco_medio == pytest.approx((50 * 58.40 + 30 * 70) / 80)
    assert pos.data_compra == date(2025, 5, 20) and len(carteira.compras(pid)) == 2


def test_migracao_nao_duplica_compras(tmp_path):
    caminho = str(tmp_path / "m.db")
    c = Carteira(Db(caminho))
    pid = nova(c)
    c.adicionar_compra(pid, date(2026, 3, 10), 50, 45.0)
    c2 = Carteira(Db(caminho))  # reabrir roda _migrar de novo
    assert len(c2.compras(pid)) == 2 and c2.obter(pid).quantidade == 150
