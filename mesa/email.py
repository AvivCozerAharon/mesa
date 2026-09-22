"""E-mail do briefing (SMTP com STARTTLS). Sem SMTP configurado é no-op — e diz isso no log."""
import logging
import smtplib
from email.message import EmailMessage

from mesa.config import Config

log = logging.getLogger("mesa.email")


def configurado(cfg: Config) -> bool:
    return bool(cfg.smtp_host and cfg.smtp_usuario and cfg.smtp_senha and cfg.email_destino)


def enviar(cfg: Config, assunto: str, corpo: str, smtp_factory=None) -> bool:
    if not configurado(cfg):
        log.info("e-mail nao configurado; pulando")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = assunto, cfg.smtp_usuario, cfg.email_destino
    msg.set_content(corpo)
    factory = smtp_factory or (lambda: smtplib.SMTP(cfg.smtp_host, cfg.smtp_porta, timeout=30))
    with factory() as s:
        s.starttls()
        s.login(cfg.smtp_usuario, cfg.smtp_senha)
        s.send_message(msg)
    return True


def corpo_briefing(briefing: dict, disparos: list[dict], resumo: dict) -> str:
    linhas = [f"mesa — briefing de {briefing.get('data')}", ""]
    cart = (briefing.get("carteira") or {}).get("saida") or {}
    if cart:
        linhas += [cart.get("resumo", ""), "", "Olhar hoje: " + ", ".join(cart.get("olhar_hoje", [])), ""]
    linhas += [f"Carteira: R$ {resumo.get('valor_brl', 0):,.0f} (P&L R$ {resumo.get('pnl_brl', 0):,.0f})", ""]
    if disparos:
        linhas.append("Gatilhos disparados:")
        linhas += [f"  - {d.get('ativo', '')}: {d.get('descricao', d.get('regra'))}" for d in disparos]
        linhas.append("")
    for p in briefing.get("posicoes", []):
        s = p.get("saida")
        if not s:
            linhas += [f"{p.get('ativo')}: briefing indisponível ({p.get('erro') or 'sem saída válida'})", ""]
            continue
        linhas += [f"{p.get('ativo')} — tese: {s.get('tese_continua')}", f"  {s.get('situacao')}", f"  {s.get('justificativa')}"]
        linhas += [f"  • {x}" for x in s.get("pontos_de_atencao", [])]
        linhas.append("")
    linhas.append(f"IA: {briefing.get('tokens_in', 0)} tokens in, {briefing.get('tokens_out', 0)} out, US$ {briefing.get('custo_usd', 0):.4f}")
    return "\n".join(linhas)
