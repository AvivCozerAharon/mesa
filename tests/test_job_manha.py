import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from mesa import job, noticias
from mesa.armazenamento import Db
from mesa.carteira import Carteira
from mesa.ia import IAIndisponivel
from tests.test_api_job import ambiente  # noqa: F401 - fixture reutilizada

BRT = ZoneInfo("America/Sao_Paulo")


class FakeIA:
    modelo = "fake"

    def __init__(self):
        self.chamadas = 0

    def completar(self, sistema, usuario):
        self.chamadas += 1
        e = json.loads(usuario)
        if "fatos" in e:
            n = [x["id"] for x in e["noticias"]]
            tese = "revisar" if any("recuperação" in x["titulo"] for x in e["noticias"]) else ("sim" if n or e["gatilhos_disparados"] else "nao_avaliavel")
            just = f"Conforme [{n[0]}]." if n else "Sem novidades."
            return json.dumps({"situacao": f"Preço em {e['fatos'].get('preco_ou_cota_ultimo')}.", "tese_continua": tese,
                               "justificativa": just, "citacoes": n[:1], "pontos_de_atencao": []}), 500, 100, 20
        return json.dumps({"resumo": f"Carteira em {e['carteira']['valor_brl']}.", "olhar_hoje": ["PETR4"], "citacoes": []}), 300, 50, 10


def test_manha_so_briefing_encadeia_etapas(ambiente, monkeypatch):  # noqa: F811
    cfg, db, cart, pid = ambiente
    cart.atualizar(pid, busca="PETR4;Petrobras")
    job.calcular(cfg, db, job.__dict__["Consulta"](cfg.dados_dir), cart, datetime(2026, 9, 22, 7, tzinfo=BRT))
    rss = ("<rss><channel><item><title>Petrobras entra em recuperação judicial?</title><link>https://x/1</link>"
           "<pubDate>Tue, 22 Sep 2026 09:00:00 GMT</pubDate></item></channel></rss>").encode()
    monkeypatch.setattr(noticias, "coletar_rss", lambda termos, **k: noticias._parse(rss, "PETR4"))
    fake = FakeIA()
    out = job.manha(cfg, datetime(2026, 9, 22, 7, 30, tzinfo=BRT), cliente_ia=fake, so_briefing=True)
    assert out["noticias"]["ok"] and out["noticias"]["novas"] == 1
    assert out["gatilhos"]["ok"] and any(d["regra"] == "noticia_contem" for d in out["gatilhos"]["novos"])
    assert out["briefing"]["ok"] and out["briefing"]["n"] == 2 and out["briefing"]["carteira_valida"]
    assert out["email"]["ok"] and out["email"]["enviado"] is False  # smtp nao configurado
    petr = next(r for r in out["briefing"]["posicoes"] if r["ativo"] == "PETR4")
    assert petr["tese_continua"] == "revisar"
    # segunda rodada no mesmo dia: nada novo, briefings reaproveitados, IA nao chamada
    chamadas = fake.chamadas
    out2 = job.manha(cfg, datetime(2026, 9, 22, 8, 0, tzinfo=BRT), cliente_ia=fake, so_briefing=True)
    assert out2["gatilhos"]["n"] == 0 and fake.chamadas == chamadas
    assert all(r["reaproveitado"] for r in out2["briefing"]["posicoes"])
    fontes = {c["fonte"] for c in db.ultimas_coletas()}
    assert {"noticias", "gatilhos", "briefing", "email"} <= fontes


def test_manha_ia_indisponivel_registra_e_segue(ambiente, monkeypatch):  # noqa: F811
    cfg, db, cart, pid = ambiente
    monkeypatch.setattr(noticias, "coletar_rss", lambda termos, **k: [])

    class Fora:
        modelo = "x"

        def completar(self, s, u):
            raise IAIndisponivel("quota")
    out = job.manha(cfg, datetime(2026, 9, 22, 7, 30, tzinfo=BRT), cliente_ia=Fora(), so_briefing=True)
    assert out["briefing"]["ok"] and out["briefing"]["n"] == 0 and out["email"]["ok"]
    assert all(r["erro"] == "quota" for r in out["briefing"]["posicoes"])
