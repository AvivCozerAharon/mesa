from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from mesa.calendario import dias_uteis, eh_dia_util, pregao_anterior, ultimo_pregao
from mesa.config import Config

BRT, ET = ZoneInfo("America/Sao_Paulo"), ZoneInfo("America/New_York")


def test_ultimo_pregao_antes_e_depois_do_fechamento():
    assert ultimo_pregao("B3", datetime(2026, 9, 21, 9, 0, tzinfo=BRT)) == date(2026, 9, 18)  # segunda de manha
    assert ultimo_pregao("B3", datetime(2026, 9, 21, 18, 30, tzinfo=BRT)) == date(2026, 9, 21)
    assert ultimo_pregao("NYSE", datetime(2026, 9, 7, 12, 0, tzinfo=ET)) == date(2026, 9, 4)  # Labor Day
    # 16:10 ET = 17:10 BRT: NYSE ja fechou, B3 ainda nao
    t = datetime(2026, 9, 21, 16, 10, tzinfo=ET)
    assert ultimo_pregao("NYSE", t) == date(2026, 9, 21) and ultimo_pregao("B3", t) == date(2026, 9, 18)


def test_feriado_de_um_mercado_so():
    d = date(2026, 11, 20)  # Consciencia Negra: B3 fechada, NYSE aberta
    assert not eh_dia_util("B3", d) and eh_dia_util("NYSE", d)
    d2 = date(2026, 11, 26)  # Thanksgiving
    assert eh_dia_util("B3", d2) and not eh_dia_util("NYSE", d2)


def test_dias_uteis_e_pregao_anterior():
    ds = dias_uteis("B3", date(2026, 9, 4), date(2026, 9, 9))  # 7/9 feriado, 5-6 fim de semana
    assert ds == [date(2026, 9, 4), date(2026, 9, 8), date(2026, 9, 9)]
    assert pregao_anterior("B3", date(2026, 9, 8)) == date(2026, 9, 4)


def test_ultimo_pregao_exige_fuso():
    with pytest.raises(ValueError):
        ultimo_pregao("B3", datetime(2026, 9, 21, 9, 0))


def test_config_do_ambiente():
    c = Config.do_ambiente({"MESA_PORTA": "9000", "OPENAI_API_KEY": "sk-x", "MESA_SCHEDULER": "0"})
    assert c.porta == 9000 and c.openai_api_key == "sk-x" and c.scheduler is False
    assert Config.do_ambiente({}).porta == 8100
