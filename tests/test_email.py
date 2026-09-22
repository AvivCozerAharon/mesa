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
