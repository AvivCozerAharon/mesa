from mesa import email as em
from mesa.config import Config


class SmtpFake:
    enviados = []

    def __init__(self):
        self.ops = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.ops.append("tls")

    def login(self, u, s):
        self.ops.append(("login", u))

    def send_message(self, msg):
        SmtpFake.enviados.append(msg)


def test_enviar_sem_config_e_com_config():
    assert em.enviar(Config(), "x", "y") is False
    cfg = Config(smtp_host="smtp.gmail.com", smtp_usuario="a@gmail.com", smtp_senha="app", email_destino="b@x.com")
    assert em.enviar(cfg, "mesa — briefing", "corpo", smtp_factory=SmtpFake) is True
    m = SmtpFake.enviados[-1]
    assert m["To"] == "b@x.com" and m["Subject"] == "mesa — briefing" and "corpo" in m.get_content()


def test_corpo_briefing():
    br = {"data": "2026-09-22", "carteira": {"saida": {"resumo": "Carteira ok.", "olhar_hoje": ["PETR4"]}},
          "posicoes": [{"ativo": "PETR4", "saida": {"tese_continua": "sim", "situacao": "s", "justificativa": "j", "leitura_fundamentos": "f", "pontos_de_atencao": ["p"]}},
                       {"ativo": "X", "saida": None, "erro": "quota"}], "tokens_in": 10, "tokens_out": 5, "custo_usd": 0.001}
    corpo = em.corpo_briefing(br, [{"ativo": "PETR4", "descricao": "na mínima"}], {"valor_brl": 1000, "pnl_brl": 10})
    assert "Olhar hoje: PETR4" in corpo and "Fundamentos: f" in corpo and "na mínima" in corpo and "indisponível (quota)" in corpo


BR = {"data": "2026-09-22", "carteira": {"saida": {"resumo": "Carteira ok.", "olhar_hoje": ["PETR4"]}},
      "posicoes": [{"ativo": "PETR4", "entrada": {"noticias": [{"id": "N1", "titulo": "Petrobras anuncia dividendos", "fonte": "Valor", "data": "2026-09-21"},
                                                               {"id": "N2", "titulo": "Outra", "fonte": "", "data": ""}]},
                    "saida": {"tese_continua": "revisar", "situacao": "s <b>", "justificativa": "Dividendo [N1].", "leitura_fundamentos": "f",
                              "pontos_de_atencao": ["p [N1]"], "citacoes": ["N1"]}},
                   {"ativo": "X", "saida": None, "erro": "quota"}], "tokens_in": 10, "tokens_out": 5, "custo_usd": 0.001}
URLS = {"Petrobras anuncia dividendos": "https://valor.com/x"}
GERAIS = [{"titulo": "Copom mantém Selic", "fonte": "G1", "url": "https://g1.com/y"}]
RES = {"valor_brl": 27627.1, "custo_brl": 25000.0, "pnl_brl": 2627.1, "sem_preco": ["Tesouro IPCA+ 2035"]}
VAL = [{"ativo": "PETR4", "peso": 60.0, "valor_brl": 16000.0, "pnl_pct": 49.5}, {"ativo": "X", "peso": None, "valor_brl": None, "pnl_pct": None}]


def test_html_briefing_links_e_noticias():
    h = em.html_briefing(BR, [], RES, VAL, GERAIS, URLS)
    assert 'href="https://valor.com/x"' in h and "[N1]</a>" in h and "Copom mantém Selic" in h and "https://g1.com/y" in h
    assert "s &lt;b&gt;" in h and "revisar" in h and "R$ 27.627" in h and "+10,5%" in h and "Sem preço atual: Tesouro IPCA+ 2035" in h
    assert "Outra" not in h  # so as citadas entram no bloco da posicao
    assert "indisponível (quota)" in h and "49,5%" in h
    txt = em.corpo_briefing(BR, [], RES, GERAIS, URLS)
    assert "[N1] Petrobras anuncia dividendos (Valor) https://valor.com/x" in txt and "Notícias do dia:" in txt and "R$ 27.627" in txt


def test_enviar_html_alternativo():
    cfg = Config(smtp_host="h", smtp_usuario="a@gmail.com", smtp_senha="app", email_destino="b@x.com")
    assert em.enviar(cfg, "s", "texto", smtp_factory=SmtpFake, html_corpo="<b>html</b>") is True
    m = SmtpFake.enviados[-1]
    assert m.get_content_type() == "multipart/alternative" and m.get_body(("html",)).get_content().strip() == "<b>html</b>"
