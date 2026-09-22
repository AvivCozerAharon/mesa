import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from mesa.fontes import Coleta, Contexto, executar
from mesa.fontes.bcb import SGS, parse_serie
from mesa.fontes.cvm import Cadastro, InformeDiario, escolher_pares, meses_ate, parse_cadastro
from mesa.fontes.tesouro import TesouroDireto
from mesa.fontes.treasury_us import Treasury
from mesa.fontes.yahoo import Yahoo, normalizar

FX = Path(__file__).parent / "fixtures"
BRT = ZoneInfo("America/Sao_Paulo")
AGORA = datetime(2026, 9, 21, 9, 0, tzinfo=BRT)  # segunda de manha: ultimo pregao B3 = 18/9


class Resp:
    def __init__(self, content: bytes):
        self.content = content
        self.text = content.decode("latin-1")

    def json(self):
        return json.loads(self.content)


def ctx(tmp_path, get=None, tickers=(), cnpjs=()):
    return Contexto(agora=AGORA, tickers=list(tickers), cnpjs=list(cnpjs), dados_dir=str(tmp_path),
                    anos_precos=1, meses_cvm=1, http_get=get)


def _yf_frame():
    idx = pd.to_datetime(["2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22"])
    cols = pd.MultiIndex.from_product([["PETR4.SA", "BRL=X"], ["Open", "High", "Low", "Close", "Adj Close", "Volume"]])
    df = pd.DataFrame(1.0, index=idx, columns=cols)
    df[("PETR4.SA", "Close")] = [48, 48.5, 48.0, float("nan")]
    df[("BRL=X", "Close")] = [5.1, 5.12, 5.14, 5.11]
    return df


def test_yahoo_descarta_linhas_do_futuro(tmp_path):
    c = ctx(tmp_path, tickers=[("PETR4", "B3")])
    df, col = executar(Yahoo(download=lambda *a, **k: _yf_frame(), incluir_benchmarks=False), c)
    assert col.ok and sorted(df["data"]) == [date(2026, 9, 17), date(2026, 9, 18)]  # 21/9 ainda nao fechou
    c2 = ctx(tmp_path, tickers=[])
    df2 = normalizar(_yf_frame(), {"BRL=X": ("USDBRL", "FX")}, c2)
    assert max(df2["data"]) == date(2026, 9, 21)  # cambio: aceita ate hoje, descarta amanha


def test_executar_transforma_excecao_em_coleta(tmp_path):
    class Quebrada:
        nome, tabela = "x", "precos"

        def coletar(self, ctx):
            raise RuntimeError("boom")
    df, col = executar(Quebrada(), ctx(tmp_path))
    assert df.empty and col.ok is False and "boom" in col.erro and col.duracao_ms >= 0


def test_cvm_cadastro_e_pares(tmp_path):
    get = lambda url, **k: Resp((FX / "cad_fi.csv").read_bytes())  # noqa: E731
    df, col = executar(Cadastro(), ctx(tmp_path, get))
    assert col.ok and len(df) == 4 and df.set_index("cnpj").loc["11222333000181", "taxa_adm"] == 2.0
    pares = escolher_pares(df, ["11222333000181"])
    assert pares == {"99887766000155"}  # PL minimo exclui o pequeno; classe exclui acoes; o proprio fundo e alvo, nao par


def test_cvm_informe_filtra_carteira_e_pares(tmp_path):
    def get(url, **k):
        if url.endswith("cad_fi.csv"):
            return Resp((FX / "cad_fi.csv").read_bytes())
        return Resp((FX / "inf_diario_fi_202608.zip").read_bytes())
    c = ctx(tmp_path, get, cnpjs=["11.222.333/0001-81"])
    c.agora = datetime(2026, 8, 20, 9, 0, tzinfo=BRT)
    fonte = InformeDiario(cadastro_fn=lambda: parse_cadastro((FX / "cad_fi.csv").read_text(encoding="latin-1")))
    df, col = executar(fonte, c)
    assert col.ok and set(df["cnpj"]) == {"11222333000181", "99887766000155"} and len(df) == 3
    assert (tmp_path / "cvm" / "raw" / "inf_diario_fi_202608.zip").exists()
    assert col.detalhe["pares"] == 1


def test_meses_ate():
    assert meses_ate(date(2026, 2, 10), 3) == ["202512", "202601", "202602"]


def test_bcb_parse_e_serie_fora(tmp_path):
    assert parse_serie("CDI", [{"data": "18/09/2026", "valor": "0.050788"}])["valor"].iloc[0] == 0.050788

    def get(url, **k):
        if "sgs.433" in url:
            raise RuntimeError("fora")
        return Resp(b'[{"data":"18/09/2026","valor":"0.05"}]')
    df, col = executar(SGS(), ctx(tmp_path, get))
    assert set(df["nome"]) == {"CDI", "SELIC"} and col.ok and "IPCA" in col.erro


def test_tesouro_e_treasury(tmp_path):
    df, col = executar(TesouroDireto(), ctx(tmp_path, lambda u, **k: Resp((FX / "tesouro.csv").read_bytes())))
    assert col.ok and len(df) == 2 and df["taxa"].max() == 7.12  # 2019 fica fora (anos_precos=1)
    df2, col2 = executar(Treasury(), ctx(tmp_path, lambda u, **k: Resp((FX / "treasury.csv").read_bytes())))
    assert col2.ok and df2[(df2["data"] == date(2026, 9, 21)) & (df2["vencimento"] == 10)]["taxa"].iloc[0] == 4.96


@pytest.mark.integration
def test_rede_real(tmp_path):
    c = Contexto(agora=datetime.now(BRT), tickers=[("PETR4", "B3"), ("AAPL", "US")], cnpjs=[], dados_dir=str(tmp_path), anos_precos=1)
    df, col = executar(Yahoo(), c)
    assert col.ok and {"PETR4", "AAPL", "IBOV", "USDBRL"} <= set(df["ativo"])
    df, col = executar(SGS(), c)
    assert col.ok and "CDI" in set(df["nome"])
