from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from mesa import job
from mesa.api import criar_app
from mesa.armazenamento import Consulta, Db, gravar_parquet
from mesa.carteira import Carteira, Posicao
from mesa.config import Config
from mesa.fontes import Contexto

BRT = ZoneInfo("America/Sao_Paulo")


def _precos(ativo, mercado, n=500, base=100.0):
    idx = pd.bdate_range(end="2026-09-18", periods=n)
    return pd.DataFrame({"ativo": ativo, "mercado": mercado, "data": [d.date() for d in idx],
                         "abertura": base, "maxima": base, "minima": base,
                         "fechamento": [base * 1.001 ** i for i in range(n)], "ajustado": [base * 1.001 ** i for i in range(n)],
                         "volume": 1.0, "fonte": "fixture"})


@pytest.fixture
def ambiente(tmp_path):
    cfg = Config(dados_dir=str(tmp_path), db_path=str(tmp_path / "m.db"), scheduler=False)
    db = Db(cfg.db_path)
    cart = Carteira(db)
    pid = cart.criar(Posicao("", "acao", "PETR4", 100, 30.0, date(2025, 1, 10)), tese="petróleo")
    cart.criar(Posicao("", "acao_us", "AAPL", 2, 200.0, date(2025, 6, 1)))
    df = pd.concat([_precos("PETR4", "B3"), _precos("AAPL", "US", base=200), _precos("IBOV", "B3", base=100000),
                    _precos("SP500", "NYSE", base=5000), _precos("USDBRL", "FX", base=5.0)])
    gravar_parquet(cfg.dados_dir, "precos", date(2026, 9, 18), df)
    cdi = pd.DataFrame({"nome": "CDI", "data": [d.date() for d in pd.bdate_range(end="2026-09-18", periods=300)], "valor": 0.05})
    gravar_parquet(cfg.dados_dir, "benchmarks", date(2026, 9, 18), cdi)
    return cfg, db, cart, pid


def test_calcular_grava_metricas_e_valoriza(ambiente):
    cfg, db, cart, pid = ambiente
    res = job.calcular(cfg, db, Consulta(cfg.dados_dir), cart, datetime(2026, 9, 21, 9, tzinfo=BRT))
    assert res["calculadas"] == 2 and res["valor_brl"] > 0
    m = db.metricas_recentes()[pid]
    assert m["benchmark"] == "IBOV" and m["mm200"] and m["retornos"]["desde_compra"] is not None


def test_coletar_registra_erro_e_segue(ambiente):
    cfg, db, cart, _ = ambiente

    class Quebrada:
        nome, tabela = "quebrada", "precos"

        def coletar(self, ctx):
            raise RuntimeError("fora do ar")

    class Boa:
        nome, tabela = "boa", "benchmarks"

        def coletar(self, ctx):
            from mesa.fontes import Coleta
            return pd.DataFrame({"nome": ["X"], "data": [date(2026, 9, 18)], "valor": [1.0]}), Coleta("boa", True)
    ctx = Contexto(agora=datetime(2026, 9, 21, 9, tzinfo=BRT), tickers=[], cnpjs=[], dados_dir=cfg.dados_dir)
    res = job.coletar(cfg, db, [Quebrada(), Boa()], ctx)
    assert res[0]["ok"] is False and res[1]["ok"] is True
    coletas = db.ultimas_coletas()
    assert {c["fonte"] for c in coletas} >= {"quebrada", "boa"} and any(c["erro"] for c in coletas)
    assert not Consulta(cfg.dados_dir).benchmark("X").empty


def test_api_crud_e_carteira(ambiente):
    cfg, db, cart, pid = ambiente
    job.calcular(cfg, db, Consulta(cfg.dados_dir), cart, datetime(2026, 9, 21, 9, tzinfo=BRT))
    cli = TestClient(criar_app(cfg, db, Consulta(cfg.dados_dir)))
    assert cli.get("/saude").json()["posicoes"] == 2
    c = cli.get("/carteira").json()
    assert c["resumo"]["valor_brl"] > 0 and c["posicoes"][0]["metricas"]["benchmark"]
    petr = next(p for p in c["posicoes"] if p["ativo"] == "PETR4")
    assert petr["tese"] == "petróleo" and petr["idade_dias"] == 3
    r = cli.post("/posicoes", json={"tipo": "fundo", "identificador": "11.222.333/0001-81", "quantidade": 10, "preco_medio": 1.5,
                                    "data_compra": "2026-01-05", "ativo": "Fundo X", "tese": "gestor"})
    assert r.status_code == 201 and r.json()["identificador"] == "11222333000181"
    fid = r.json()["id"]
    assert cli.post("/posicoes", json={"tipo": "acao", "identificador": "PETR", "quantidade": 1, "preco_medio": 1, "data_compra": "2026-01-05"}).status_code == 422
    r = cli.put(f"/posicoes/{fid}", json={"quantidade": 20, "tese": "gestor; trocou o CIO, revisar"})
    assert r.json()["quantidade"] == 20 and len(cli.get(f"/posicoes/{fid}/tese").json()) == 2
    assert cli.get(f"/ativo/{pid}").json()["serie"][-1]["data"] == "2026-09-18"
    assert cli.delete(f"/posicoes/{fid}").json()["ok"] and len(cli.get("/posicoes").json()) == 2
    assert cli.get("/macro").json()["benchmarks"]["IBOV"]["ultimo"] > 0
    assert cli.get("/operacao").status_code == 200
    assert cli.post("/job/xyz").status_code == 404


def test_api_senha(tmp_path):
    cfg = Config(dados_dir=str(tmp_path), db_path=str(tmp_path / "m.db"), senha="segredo", scheduler=False)
    cli = TestClient(criar_app(cfg))
    assert cli.get("/carteira").status_code == 401
    assert cli.post("/login", json={"senha": "errada"}).status_code == 401
    assert cli.post("/login", json={"senha": "segredo"}).status_code == 200
    assert cli.get("/carteira").status_code == 200
