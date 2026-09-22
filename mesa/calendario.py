"""Calendário por mercado: dias úteis, fechamento e "último pregão".

Toda data de mercado passa por aqui. `date.today()` solto está proibido no resto do código: "ontem"
não é o mesmo dia útil na B3 e na NYSE (feriado brasileiro com bolsa americana aberta é o bug
clássico), e o Yahoo devolve linha de câmbio datada de amanhã porque o dia vira na Ásia.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

FUSO = {"B3": ZoneInfo("America/Sao_Paulo"), "NYSE": ZoneInfo("America/New_York")}
FECHAMENTO = {"B3": (18, 0), "NYSE": (16, 0)}  # hora local do mercado

# B3 (ANBIMA): feriados nacionais + Carnaval, Sexta-feira Santa, Corpus Christi, Consciência Negra (desde 2024)
FERIADOS_B3 = {
    2024: ["01-01", "02-12", "02-13", "03-29", "04-21", "05-01", "05-30", "09-07", "10-12", "11-02", "11-15",
           "11-20", "12-24", "12-25", "12-31"],
    2025: ["01-01", "03-03", "03-04", "04-18", "04-21", "05-01", "06-19", "09-07", "10-12", "11-02", "11-15",
           "11-20", "12-24", "12-25", "12-31"],
    2026: ["01-01", "02-16", "02-17", "04-03", "04-21", "05-01", "06-04", "09-07", "10-12", "11-02", "11-15",
           "11-20", "12-24", "12-25", "12-31"],
    2027: ["01-01", "02-08", "02-09", "03-26", "04-21", "05-01", "05-27", "09-07", "10-12", "11-02", "11-15",
           "11-20", "12-24", "12-25", "12-31"],
}
# NYSE: New Year, MLK, Presidents, Good Friday, Memorial, Juneteenth, Independence, Labor, Thanksgiving, Christmas
FERIADOS_NYSE = {
    2024: ["01-01", "01-15", "02-19", "03-29", "05-27", "06-19", "07-04", "09-02", "11-28", "12-25"],
    2025: ["01-01", "01-09", "01-20", "02-17", "04-18", "05-26", "06-19", "07-04", "09-01", "11-27", "12-25"],
    2026: ["01-01", "01-19", "02-16", "04-03", "05-25", "06-19", "07-03", "09-07", "11-26", "12-25"],
    2027: ["01-01", "01-18", "02-15", "03-26", "05-31", "06-18", "07-05", "09-06", "11-25", "12-24"],
}


def _feriados(mercado: str) -> set[date]:
    tabela = FERIADOS_B3 if mercado == "B3" else FERIADOS_NYSE
    return {date.fromisoformat(f"{ano}-{md}") for ano, lista in tabela.items() for md in lista}


def eh_dia_util(mercado: str, d: date) -> bool:
    return d.weekday() < 5 and d not in _feriados(mercado)


def dias_uteis(mercado: str, inicio: date, fim: date) -> list[date]:
    out, d = [], inicio
    while d <= fim:
        if eh_dia_util(mercado, d):
            out.append(d)
        d += timedelta(days=1)
    return out


def pregao_anterior(mercado: str, d: date) -> date:
    d -= timedelta(days=1)
    while not eh_dia_util(mercado, d):
        d -= timedelta(days=1)
    return d


def ultimo_pregao(mercado: str, agora: datetime) -> date:
    """Último dia cujo fechamento já aconteceu, na hora local do mercado. `agora` precisa ter fuso."""
    if agora.tzinfo is None:
        raise ValueError("agora precisa ter fuso horário")
    local = agora.astimezone(FUSO[mercado])
    d = local.date()
    h, m = FECHAMENTO[mercado]
    fechou = eh_dia_util(mercado, d) and (local.hour, local.minute) >= (h, m)
    return d if fechou else pregao_anterior(mercado, d)


def agora_brt() -> datetime:
    return datetime.now(FUSO["B3"])


def hoje_brt() -> date:
    return agora_brt().date()
