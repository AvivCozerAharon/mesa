import json
from datetime import date

import pytest

from mesa import ia
from mesa.armazenamento import Db
from mesa.config import Config

POS = {"id": 1, "ativo": "PETR4", "tipo": "acao", "preco_medio": 32.1, "data_compra": "2025-02-14"}
MET = {"ultimo": 48.0, "data_ultimo": "2026-09-21", "vs_preco_medio_pct": 49.53, "retornos": {"1m": 3.7, "12m": 69.5},
       "benchmark": "IBOV", "bench_retornos": {"1m": 1.2, "12m": 20.0}, "excesso": {"1m": 2.5, "12m": 49.5},
       "drawdown": {"atual": -4.8, "maximo": -30.2}, "vol_30d": 22.1, "vol_252d": 28.4, "mm50": 46.0, "mm200": 41.0,
       "s52": {"pct_do_topo": -5.0, "pct_do_fundo": 40.0}, "beta_12m": 1.1}
NOT = [{"id": 10, "titulo": "Petrobras anuncia dividendos de R$ 1,50 por ação", "fonte": "Valor", "publicada_em": "2026-09-21T10:00:00"}]
DISP = [{"regra": "min_52s", "descricao": "na mínima", "detalhe": {}}]


def entrada(noticias=NOT, disparos=(), tese="Petróleo alto e dividendos; sair se a política de preços mudar"):
    return ia.montar_entrada(POS, tese, MET, noticias, list(disparos), date(2026, 9, 22))


BOA = {"situacao": "PETR4 a 48.0, 49.5% acima do preço médio; 12 meses +69.5% contra +20.0% do IBOV; drawdown atual -4.8%.",
       "tese_continua": "sim", "justificativa": "Dividendo anunciado reforça a tese [N1].", "citacoes": ["N1"],
       "pontos_de_atencao": ["A 5.0% do topo de 52 semanas"]}


def test_montar_entrada_arredonda_e_numera():
    e = entrada()
    assert e["fatos"]["retorno_desde_compra_pct"] == 49.5 and e["noticias"][0]["id"] == "N1" and e["tese"]["texto"].startswith("Petróleo")
    assert "beta_12m" in e["fatos"] and "pct_do_benchmark" not in e["fatos"]


def test_validar_aceita_boa_e_rejeita_ruins():
    e = entrada()
    assert ia.validar(e, BOA) == (True, [])
    ok, p = ia.validar(e, {**BOA, "citacoes": ["N7"]})
    assert not ok and "inexistentes" in p[0]
    ok, p = ia.validar(e, {**BOA, "justificativa": "Como diz [N3], reforça."})
    assert not ok and any("inexistente" in x for x in p)
    ok, p = ia.validar(e, {**BOA, "situacao": "PETR4 caiu 37.0% no mês."})
    assert not ok and any("37.0" in x for x in p)
    ok, p = ia.validar(e, {**BOA, "pontos_de_atencao": ["Venda metade agora"]})
    assert not ok and any("recomenda" in x for x in p)
    ok, p = ia.validar(e, {**BOA, "tese_continua": "talvez"})
    assert not ok
    ok, p = ia.validar(entrada(noticias=[]), {**BOA, "citacoes": [], "justificativa": "ok", "tese_continua": "sim"})
    assert not ok and any("nao_avaliavel" in x for x in p)
    assert ia.validar(e, "texto")[0] is False
    ok, _ = ia.validar(e, {**BOA, "situacao": "Carteira vale R$ 22.3 mil (22308 na entrada)"})  # numero fora: 22308 nao existe aqui
    assert not ok


class Fake:
    modelo = "fake"

    def __init__(self, respostas):
        self.respostas, self.chamadas = list(respostas), 0

    def completar(self, sistema, usuario):
        self.chamadas += 1
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return json.dumps(r, ensure_ascii=False), 1000, 200, 50


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "m.db"))


def test_gerar_reexecuta_uma_vez_e_grava_custo(db):
    cfg = Config(openai_preco_in=1.0, openai_preco_out=4.0)
    cli = Fake([{**BOA, "citacoes": ["N9"]}, BOA])
    r = ia.gerar_briefing_posicao(cli, db, cfg, POS, entrada(), date(2026, 9, 22))
    assert r["valido"] == 1 and cli.chamadas == 2 and r["tokens_in"] == 2000
    assert r["custo_usd"] == pytest.approx(2000 / 1e6 * 1.0 + 400 / 1e6 * 4.0)
    # mesma entrada no mesmo dia: reaproveita sem chamar
    r2 = ia.gerar_briefing_posicao(cli, db, cfg, POS, entrada(), date(2026, 9, 22))
    assert r2["reaproveitado"] and cli.chamadas == 2
    dia = ia.briefing_do_dia(db, date(2026, 9, 22))
    assert dia["posicoes"][0]["saida"]["tese_continua"] == "sim" and dia["custo_usd"] > 0


def test_gerar_invalido_duas_vezes_e_indisponivel(db):
    cfg = Config()
    r = ia.gerar_briefing_posicao(Fake([{**BOA, "citacoes": ["N9"]}, {**BOA, "citacoes": ["N8"]}]), db, cfg, POS, entrada(), date(2026, 9, 22))
    assert r["valido"] == 0 and "inválido" in r["erro"] and r["saida"] is None
    r2 = ia.gerar_briefing_posicao(Fake([ia.IAIndisponivel("quota")]), db, cfg, {**POS, "id": 2}, entrada(), date(2026, 9, 22))
    assert r2["valido"] == 0 and r2["erro"] == "quota"
    dia = ia.briefing_do_dia(db, date(2026, 9, 22))
    assert len(dia["posicoes"]) == 2 and all(p["saida"] is None for p in dia["posicoes"])


def test_briefing_carteira(db):
    ent = {"carteira": {"valor_brl": 22308.6, "pnl_pct": 12.3}, "posicoes": [{"ativo": "PETR4", "tese_continua": "sim"}], "gatilhos": []}
    cli = Fake([{"resumo": "Carteira em 22308.6 reais, +12.3%.", "olhar_hoje": ["PETR4"], "citacoes": []}])
    r = ia.gerar_briefing_carteira(cli, db, Config(), ent, date(2026, 9, 22))
    assert r["valido"] == 1 and ia.briefing_do_dia(db)["carteira"]["saida"]["olhar_hoje"] == ["PETR4"]


def test_extrair_termos_do_mandato():
    cli = Fake([{"termos": ["debêntures incentivadas", "crédito privado", "crédito privado", "mercado", "x" * 50, "CDI"]}])
    assert ia.extrair_termos(cli, "Compra debêntures incentivadas e crédito privado high grade, meta CDI+2%") == ["debêntures incentivadas", "crédito privado", "mercado", "CDI"]
    assert ia.extrair_termos(cli, "   ") == [] and cli.chamadas == 1
    assert ia.montar_entrada({**POS, "mandato": "crédito privado"}, None, MET, [], [], date(2026, 9, 22))["mandato"] == "crédito privado"
