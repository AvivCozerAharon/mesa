from datetime import date, timedelta

from mesa.armazenamento import Db, agora_utc
from mesa.carteira import Posicao
from mesa.noticias import casar, coletar_rss, gerais, normalizar_titulo, recentes, salvar, termos, url_canonica

PETR = Posicao("", "acao", "PETR4", 1, 1, date(2025, 1, 1), busca="PETR4;Petrobras")
VALE = Posicao("", "acao", "VALE3", 1, 1, date(2025, 1, 1), busca="VALE3;Vale S.A.")
VERDE = Posicao("CSHG Verde 30 FIC FIM", "fundo", "22.215.116/0001-80", 1, 1, date(2025, 1, 1), busca="Verde Asset")
AAPL = Posicao("", "acao_us", "AAPL", 1, 1, date(2025, 1, 1), busca="AAPL;Apple")
POS = [PETR, VALE, VERDE, AAPL]

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Petrobras (PETR4) paga dividendos - Investidor10</title><link>https://x.com/a?utm_source=g&amp;id=1</link>
<pubDate>Mon, 21 Sep 2026 11:19:27 GMT</pubDate><source url="https://investidor10.com.br">Investidor10</source></item>
<item><title>Vale a pena investir em CDB agora?</title><link>https://x.com/b</link><pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate></item>
<item><title>Vale anuncia recompra de ações</title><link>https://x.com/c</link><pubDate>Sun, 20 Sep 2026 10:00:00 GMT</pubDate></item>
</channel></rss>""".encode("utf-8")


def test_url_canonica_e_titulo():
    assert url_canonica("https://X.com/a?utm_source=g&id=1#frag") == "https://x.com/a?id=1"
    assert normalizar_titulo("Petróleo sobe 3%!") == "petroleo sobe 3"


def test_termos():
    assert termos(PETR) == ["PETR4", "Petrobras"]
    assert termos(Posicao("", "acao", "ITSA4", 1, 1, date(2025, 1, 1))) == ["ITSA4"]
    assert termos(Posicao("CSHG VERDE 30 FUNDO DE INVESTIMENTO EM COTAS DE FIF MULTIMERCADO", "fundo", "22.215.116/0001-80", 1, 1, date(2025, 1, 1))) == ["CSHG VERDE 30"]


def test_casar_ticker_nome_e_exclusao():
    assert casar("Vale a pena investir em CDB agora?", POS) == []
    assert casar("Vale (VALE3) anuncia dividendos", POS) == [("VALE3", "ticker", 1.0)]
    assert casar("Petrobras muda política de preços", POS) == [("PETR4", "nome", 0.8)]
    assert casar("Vale anuncia recompra", POS) == [("VALE3", "nome", 0.8)]
    assert casar("Verde Asset reduz posição em bolsa", POS) == [("22215116000180", "nome", 0.8)]
    assert casar("Apple lança iPhone; Petrobras cai", POS) == [("PETR4", "nome", 0.8), ("AAPL", "nome", 0.8)]
    assert casar("Mercado fecha em alta", POS) == []


def test_coletar_rss_parse_e_falha_isolada():
    def fetch(url):
        if "Petrobras" in url or "PETR4" in url:
            return RSS
        raise RuntimeError("fora")
    itens = coletar_rss(["PETR4", "Petrobras", "Apple"], fetch=fetch, feeds_gerais={"Geral": "https://g/feed"})
    assert len(itens) == 6  # dois termos que respondem x 3 itens; Apple e o feed geral falham em silencio
    assert itens[0]["titulo"] == "Petrobras (PETR4) paga dividendos" and itens[0]["fonte"] == "Investidor10"
    assert itens[0]["url"] == "https://x.com/a?id=1" and itens[0]["publicada_em"].startswith("2026-09-21T11:19:27")


def test_salvar_dedupe_e_recentes(tmp_path):
    db = Db(str(tmp_path / "m.db"))
    itens = coletar_rss(["PETR4"], fetch=lambda u: RSS, feeds_gerais={})
    r = salvar(db, itens, POS)
    assert r["novas"] == 3 and r["casadas"] == 2  # petrobras->PETR4, vale recompra->VALE3; "vale a pena" nao casa
    r2 = salvar(db, itens + [{"url": "https://outro.com/z", "titulo": "Vale anuncia recompra de ações", "fonte": "Y", "publicada_em": None}], POS)
    assert r2["novas"] == 0  # mesma URL e mesmo titulo em fonte diferente: ignorados
    assert [n["titulo"] for n in recentes(db, "VALE3")] == ["Vale anuncia recompra de ações"]
    assert recentes(db, "VALE3", dias=0) == []
    assert len(gerais(db, dias=3)) == 3
