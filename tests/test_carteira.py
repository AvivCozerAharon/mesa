from datetime import date

import pandas as pd
import pytest

from mesa.armazenamento import Db
from mesa.carteira import Carteira, Posicao, cnpj_valido, resumo, valorizar


@pytest.fixture
def cart(tmp_path):
    return Carteira(Db(str(tmp_path / "m.db")))


def test_validacoes():
    assert cnpj_valido("11.222.333/0001-81") and not cnpj_valido("11222333000180")
    with pytest.raises(ValueError):
        Posicao("x", "acao", "PETR", 1, 1, date(2026, 1, 1))
    with pytest.raises(ValueError):
        Posicao("x", "fundo", "11222333000180", 1, 1, date(2026, 1, 1))
    with pytest.raises(ValueError):
        Posicao("x", "acao", "PETR4", 0, 1, date(2026, 1, 1))
    with pytest.raises(ValueError):
        Posicao("x", "cripto", "BTC", 1, 1, date(2026, 1, 1))
    p = Posicao("", "acao", "petr4", 100, 30.5, "2025-03-10")
    assert p.ativo == "PETR4" and p.mercado == "B3" and p.moeda == "BRL" and p.data_compra == date(2025, 3, 10)
    assert Posicao("", "acao_us", "brk.b", 1, 1, date(2026, 1, 1)).identificador == "BRK.B"
    assert Posicao("Meu fundo", "fundo", "11.222.333/0001-81", 10, 1.5, date(2026, 1, 1)).identificador == "11222333000181"


def test_crud_e_tese_com_historico(cart):
    pid = cart.criar(Posicao("", "acao", "VALE3", 50, 60.0, date(2025, 1, 15)), tese="minério + dividendo")
    assert cart.obter(pid).ativo == "VALE3" and cart.tese(pid) == "minério + dividendo"
    cart.atualizar(pid, quantidade=80, preco_medio=62.5)
    assert cart.obter(pid).quantidade == 80
    cart.definir_tese(pid, "minério + dividendo; China desacelerando, revisar")
    assert len(cart.historico_teses(pid)) == 2 and cart.tese(pid).startswith("minério + dividendo;")
    cart.criar(Posicao("", "acao_us", "AAPL", 3, 190.0, date(2025, 6, 1)))
    assert [p.ativo for p in cart.listar()] == ["VALE3", "AAPL"]
    assert cart.tickers() == [("AAPL", "US"), ("VALE3", "B3")]
    cart.excluir(pid)
    assert [p.ativo for p in cart.listar()] == ["AAPL"] and len(cart.listar(ativas=False)) == 2
    with pytest.raises(KeyError):
        cart.atualizar(999, quantidade=1)


def test_importar_csv_aponta_linha(cart, tmp_path):
    csv = tmp_path / "c.csv"
    csv.write_text("ativo,tipo,identificador,quantidade,preco_medio,data_compra,tese\n"
                   ",acao,PETR4,100,30,2025-01-10,petróleo\n"
                   "Fundo X,fundo,11.222.333/0001-81,1000,1.2,2024-05-02,\n", encoding="utf-8")
    assert cart.importar_csv(str(csv)) == 2 and cart.cnpjs() == ["11222333000181"]
    csv.write_text("ativo,tipo,identificador,quantidade,preco_medio,data_compra,tese\n,acao,PETR,1,1,2025-01-10,\n", encoding="utf-8")
    with pytest.raises(ValueError, match="linha 2"):
        cart.importar_csv(str(csv))


def test_valorizar_converte_e_mantem_sem_preco():
    ps = [Posicao("", "acao", "PETR4", 100, 30.0, date(2025, 1, 1), id=1),
          Posicao("", "acao_us", "AAPL", 2, 200.0, date(2025, 1, 1), id=2),
          Posicao("F", "fundo", "11222333000181", 1000, 1.0, date(2025, 1, 1), id=3)]
    df = valorizar(ps, {"PETR4": (48.0, date(2026, 9, 18)), "AAPL": (250.0, date(2026, 9, 18))}, cambio=5.0, hoje=date(2026, 9, 21))
    petr, aapl, fundo = (df[df["id"] == i].iloc[0] for i in (1, 2, 3))
    assert petr["valor_brl"] == 4800 and petr["pnl_pct"] == pytest.approx(60) and petr["idade_dias"] == 3
    assert aapl["valor_brl"] == 2500 and aapl["custo_brl"] == 2000 and aapl["pnl_brl"] == 500
    assert pd.isna(fundo["valor_brl"]) and pd.isna(fundo["idade_dias"])
    assert abs(petr["peso"] + aapl["peso"] - 100) < 1e-9
    r = resumo(df)
    assert r["valor_brl"] == 7300 and r["por_moeda"] == {"BRL": 4800, "USD": 2500} and r["sem_preco"] == ["F"]
