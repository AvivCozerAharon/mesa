from datetime import date

import pandas as pd
import pytest

from mesa import busca
from mesa.armazenamento import Consulta, gravar_parquet

YAHOO = {"quotes": [
    {"symbol": "PBR", "longname": "Petróleo Brasileiro S.A. - Petrobras", "exchange": "NYQ", "quoteType": "EQUITY"},
    {"symbol": "PETR4.SA", "shortname": "PETROBRAS PN N2", "longname": "Petróleo Brasileiro S.A. - Petrobras", "exchange": "SAO", "quoteType": "EQUITY"},
    {"symbol": "HGLG11.SA", "longname": "CSHG Logística FII", "exchange": "SAO", "quoteType": "MUTUALFUND"},
    {"symbol": "BOVA11.SA", "longname": "iShares Ibovespa", "exchange": "SAO", "quoteType": "ETF"},
    {"symbol": "AAPL34.SA", "longname": "Apple BDR", "exchange": "SAO", "quoteType": "EQUITY"},
    {"symbol": "VOO", "longname": "Vanguard S&P 500 ETF", "exchange": "PCX", "quoteType": "ETF"},
    {"symbol": "PJX.MU", "longname": "Petrobras", "exchange": "MUN", "quoteType": "EQUITY"},  # Munique: fora
    {"symbol": "^BVSP", "longname": "Ibovespa", "exchange": "SAO", "quoteType": "INDEX"},     # indice: fora
]}


class Resposta:
    def __init__(self, dados):
        self._d = dados

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def get_fake(url, **kw):
    return Resposta(YAHOO)


@pytest.fixture
def consulta(tmp_path):
    gravar_parquet(str(tmp_path), "fundos_cadastro", date(2026, 9, 22), pd.DataFrame([
        {"cnpj": "22215116000180", "nome": "CSHG VERDE 30 FIC FIM", "gestor": "VERDE ASSET", "classe": "Multimercado", "pl": 932e6, "situacao": "EM FUNCIONAMENTO NORMAL"},
        {"cnpj": "11052478000103", "nome": "VERDE AM ICATU PREV FIC FIM", "gestor": "VERDE ASSET", "classe": "Previdência", "pl": 2.1e9, "situacao": "EM FUNCIONAMENTO NORMAL"},
        {"cnpj": "99999999000199", "nome": "FUNDO VERDE ENCERRADO", "gestor": "X", "classe": "Renda Fixa", "pl": 10e6, "situacao": "CANCELADA"},
    ]))
    gravar_parquet(str(tmp_path), "curvas", date(2026, 9, 22), pd.DataFrame([
        {"pais": "BR", "data": date(2026, 9, 18), "titulo": "Tesouro IPCA+", "vencimento": date(2029, 5, 15), "taxa": 7.6, "pu": 3913.82, "prazo_anos": 2.6},
        {"pais": "BR", "data": date(2026, 9, 18), "titulo": "Tesouro Selic", "vencimento": date(2031, 3, 1), "taxa": 0.1, "pu": 18000.0, "prazo_anos": 4.4},
    ]), fonte="tesouro")
    gravar_parquet(str(tmp_path), "cotas_fundos", date(2026, 9, 22), pd.DataFrame([
        {"cnpj": "22215116000180", "data": date(2026, 9, 16), "cota": 3.126273, "pl": 932e6, "captacao": 0.0, "resgate": 0.0, "cotistas": 100},
    ]))
    return Consulta(str(tmp_path))


def test_tickers_classifica_mercado_e_tipo():
    r = busca.tickers("petrobras", get=get_fake)
    por_id = {x["identificador"]: x for x in r}
    assert por_id["PETR4"]["tipo"] == "acao" and por_id["PETR4"]["mercado"] == "B3" and por_id["PETR4"]["moeda"] == "BRL"
    assert por_id["HGLG11"]["tipo"] == "fii" and por_id["BOVA11"]["tipo"] == "etf" and por_id["AAPL34"]["tipo"] == "bdr"
    assert por_id["VOO"]["tipo"] == "etf_us" and por_id["PBR"]["tipo"] == "acao_us" and por_id["PBR"]["onde"] == "NYSE"
    assert "PJX" not in por_id and "^BVSP" not in por_id  # bolsa nao coberta e indice ficam de fora
    assert busca.tickers("p", get=get_fake) == []


def test_fundos_por_nome_e_cnpj(consulta):
    r = busca.fundos(consulta, "verde")
    assert [x["identificador"] for x in r][:2] == ["11052478000103", "22215116000180"]  # maior PL primeiro
    assert "VERDE ASSET" in r[0]["detalhe"] and "PL R$ 2.100 mi" in r[0]["detalhe"]
    assert [x for x in r if x["identificador"] == "99999999000199"][0]["inativo"] is True
    assert busca.fundos(consulta, "22.215.116/0001-80")[0]["ativo"] == "CSHG VERDE 30 FIC FIM"
    assert busca.fundos(consulta, "verde 30")[0]["identificador"] == "22215116000180"  # todas as palavras
    assert busca.fundos(consulta, "ab") == []


def test_tesouro_e_busca_junta_tudo(consulta):
    assert busca.tesouro(consulta, "ipca")[0]["identificador"] == "Tesouro IPCA+ 2029"
    assert busca.tesouro(consulta, "tesouro 2029")[0]["identificador"] == "Tesouro IPCA+ 2029"
    assert busca.tesouro(consulta, "petr") == []
    r = busca.buscar(consulta, "verde", get=get_fake)
    assert {x["tipo"] for x in r["itens"]} >= {"fundo", "acao"} and r["avisos"] == []


def test_busca_sobrevive_ao_yahoo_fora(consulta):
    def explode(*a, **k):
        raise TimeoutError("yahoo")
    r = busca.buscar(consulta, "verde", get=explode)
    assert [x["tipo"] for x in r["itens"]] == ["fundo", "fundo", "fundo"] and r["avisos"] == ["tickers: TimeoutError"]


def test_cotacao_por_tipo(consulta):
    assert busca.cotacao(consulta, "fundo", "22215116000180") == {"preco": pytest.approx(3.126273), "data": "2026-09-16", "fonte": "CVM"}
    t = busca.cotacao(consulta, "tesouro", "Tesouro IPCA+ 2029")
    assert t["preco"] == pytest.approx(3913.82) and t["detalhe"] == "taxa 7.60%"
    assert busca.cotacao(consulta, "tesouro", "Tesouro IPCA+ 2045")["erro"]
    assert busca.cotacao(consulta, "acao", "PETR4", cotacao_fn=lambda t: {"PETR4": {"preco": 48.0, "moeda": "BRL"}})["preco"] == 48.0
    assert busca.cotacao(consulta, "acao", "XPTO3", cotacao_fn=lambda t: {})["erro"]
