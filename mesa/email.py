"""E-mail do briefing (SMTP com STARTTLS). Sem SMTP configurado é no-op — e diz isso no log.

A mensagem vai em duas partes: texto puro (`corpo_briefing`) e HTML (`html_briefing`). O HTML é o que
o Gmail mostra; o texto é o fallback e o que os testes conferem. Estilos inline porque cliente de
e-mail ignora <style> em boa parte dos casos.
"""
import html
import logging
import re
import smtplib
from email.message import EmailMessage

from mesa.config import Config

log = logging.getLogger("mesa.email")

COR = {"sim": "#2E7D32", "nao": "#C62828", "revisar": "#B26A00", "nao_avaliavel": "#666", "indisponivel": "#666"}
ROTULO = {"sim": "tese de pé", "nao": "tese furada", "revisar": "revisar", "nao_avaliavel": "sem base", "indisponivel": "indisponível"}
_REF = re.compile(r"\[(N\d+)\]")


def configurado(cfg: Config) -> bool:
    return bool(cfg.smtp_host and cfg.smtp_usuario and cfg.smtp_senha and cfg.email_destino)


def enviar(cfg: Config, assunto: str, corpo: str, smtp_factory=None, html_corpo: str | None = None) -> bool:
    if not configurado(cfg):
        log.info("e-mail nao configurado; pulando")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = assunto, cfg.smtp_usuario, cfg.email_destino
    msg.set_content(corpo)
    if html_corpo:
        msg.add_alternative(html_corpo, subtype="html")
    factory = smtp_factory or (lambda: smtplib.SMTP(cfg.smtp_host, cfg.smtp_porta, timeout=30))
    with factory() as s:
        s.starttls()
        s.login(cfg.smtp_usuario, cfg.smtp_senha)
        s.send_message(msg)
    return True


def brl(v) -> str:
    if v is None:
        return "–"
    s = f"{abs(v):,.0f}".replace(",", ".")
    return f"{'-' if v < 0 else ''}R$ {s}"


def pct(v) -> str:
    return "–" if v is None else f"{v:+.1f}%".replace(".", ",")


def _noticias_citadas(p: dict, urls: dict[str, str]) -> list[dict]:
    """Notícias N# da entrada que a IA citou, com a URL casada pelo título."""
    s = p.get("saida") or {}
    citadas = set(s.get("citacoes") or []) | set(_REF.findall(" ".join([s.get("justificativa", ""), s.get("situacao", "")] + list(s.get("pontos_de_atencao") or []))))
    out = []
    for n in (p.get("entrada") or {}).get("noticias", []):
        if n["id"] in citadas:
            out.append({**n, "url": urls.get(n["titulo"])})
    return out


def corpo_briefing(briefing: dict, disparos: list[dict], resumo: dict, gerais: list[dict] | None = None, urls: dict[str, str] | None = None) -> str:
    urls = urls or {}
    linhas = [f"mesa — briefing de {briefing.get('data')}", ""]
    cart = (briefing.get("carteira") or {}).get("saida") or {}
    if cart:
        linhas += [cart.get("resumo", ""), "", "Olhar hoje: " + ", ".join(cart.get("olhar_hoje", [])), ""]
    linhas += [f"Carteira: {brl(resumo.get('valor_brl', 0))} (P&L {brl(resumo.get('pnl_brl', 0))})", ""]
    if disparos:
        linhas.append("Gatilhos disparados:")
        linhas += [f"  - {d.get('ativo', '')}: {d.get('descricao', d.get('regra'))}" for d in disparos]
        linhas.append("")
    if gerais:
        linhas.append("Notícias do dia:")
        linhas += [f"  - {n['titulo']} ({n.get('fonte') or ''}) {n.get('url') or ''}".rstrip() for n in gerais]
        linhas.append("")
    for p in briefing.get("posicoes", []):
        s = p.get("saida")
        if not s:
            linhas += [f"{p.get('ativo')}: briefing indisponível ({p.get('erro') or 'sem saída válida'})", ""]
            continue
        linhas += [f"{p.get('ativo')} — tese: {s.get('tese_continua')}", f"  {s.get('situacao')}", f"  {s.get('justificativa')}"]
        if s.get("leitura_fundamentos"):
            linhas.append(f"  Fundamentos: {s['leitura_fundamentos']}")
        linhas += [f"  • {x}" for x in s.get("pontos_de_atencao", [])]
        for n in _noticias_citadas(p, urls):
            linhas.append(f"  [{n['id']}] {n['titulo']} ({n.get('fonte') or ''}) {n.get('url') or ''}".rstrip())
        linhas.append("")
    linhas.append(f"IA: {briefing.get('tokens_in', 0)} tokens in, {briefing.get('tokens_out', 0)} out, US$ {briefing.get('custo_usd', 0):.4f}")
    return "\n".join(linhas)


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _refs(texto: str, links: dict[str, str | None]) -> str:
    """[N3] vira link para a notícia (se houver URL), em cinza."""
    def sub(m):
        nid = m.group(1)
        u = links.get(nid)
        return f'<a href="{_e(u)}" style="color:#8a6d1f;text-decoration:none">[{nid}]</a>' if u else f'<span style="color:#888">[{nid}]</span>'
    return _REF.sub(sub, _e(texto))


def html_briefing(briefing: dict, disparos: list[dict], resumo: dict, val: list[dict] | None = None,
                  gerais: list[dict] | None = None, urls: dict[str, str] | None = None) -> str:
    urls, val = urls or {}, val or []
    data = briefing.get("data") or ""
    cart = (briefing.get("carteira") or {}).get("saida") or {}
    pnl = resumo.get("pnl_brl") or 0
    custo = resumo.get("custo_brl") or 0
    cor_pnl = COR["sim"] if pnl >= 0 else COR["nao"]
    css_p = "margin:0 0 8px;line-height:1.45"
    partes = [f"""<div style="max-width:640px;margin:0 auto;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#222;font-size:14px">
<div style="border-bottom:3px solid #E9B44C;padding:8px 0 10px;margin-bottom:16px">
  <span style="font-size:22px;font-weight:700;letter-spacing:.5px">mesa</span>
  <span style="color:#666;margin-left:10px">briefing de {_e(data)}</span>
</div>
<table cellpadding="0" cellspacing="0" style="width:100%;margin-bottom:14px"><tr>
  <td style="padding-right:18px"><div style="color:#666;font-size:12px">carteira</div><div style="font-size:24px;font-weight:700">{_e(brl(resumo.get('valor_brl')))}</div></td>
  <td style="padding-right:18px"><div style="color:#666;font-size:12px">resultado</div><div style="font-size:24px;font-weight:700;color:{cor_pnl}">{_e(brl(pnl))}</div></td>
  <td><div style="color:#666;font-size:12px">sobre o custo</div><div style="font-size:24px;font-weight:700;color:{cor_pnl}">{_e(pct(pnl / custo * 100 if custo else None))}</div></td>
</tr></table>"""]
    if cart:
        partes.append(f'<p style="{css_p}">{_e(cart.get("resumo", ""))}</p>')
        if cart.get("olhar_hoje"):
            chips = "".join(f'<span style="display:inline-block;background:#FFF3D6;border:1px solid #E9B44C;border-radius:3px;padding:2px 8px;margin:2px 6px 2px 0">{_e(a)}</span>' for a in cart["olhar_hoje"])
            partes.append(f'<p style="{css_p}"><span style="color:#666">Olhar hoje</span><br>{chips}</p>')
    if resumo.get("sem_preco"):
        partes.append(f'<p style="{css_p};color:#B26A00">Sem preço atual: {_e(", ".join(resumo["sem_preco"]))} — fora do valor acima.</p>')
    if val:
        linhas = "".join(
            f'<tr><td style="padding:4px 8px 4px 0;border-bottom:1px solid #eee">{_e(r.get("ativo"))}</td>'
            f'<td style="padding:4px 8px;border-bottom:1px solid #eee;text-align:right;color:#666">{_e(pct(r.get("peso")).lstrip("+"))}</td>'
            f'<td style="padding:4px 8px;border-bottom:1px solid #eee;text-align:right">{_e(brl(r.get("valor_brl")))}</td>'
            f'<td style="padding:4px 0 4px 8px;border-bottom:1px solid #eee;text-align:right;color:{COR["sim"] if (r.get("pnl_pct") or 0) >= 0 else COR["nao"]}">{_e(pct(r.get("pnl_pct")))}</td></tr>'
            for r in sorted(val, key=lambda r: -(r.get("valor_brl") or 0)))
        partes.append(f'<table cellpadding="0" cellspacing="0" style="width:100%;margin:6px 0 16px;font-size:13px"><tr style="color:#666;font-size:11px"><td>ativo</td><td style="text-align:right">peso</td><td style="text-align:right">valor</td><td style="text-align:right">desde a compra</td></tr>{linhas}</table>')
    if disparos:
        itens = "".join(f'<li>{_e(d.get("ativo", ""))}: {_e(d.get("descricao", d.get("regra")))}</li>' for d in disparos)
        partes.append(f'<div style="background:#FDECEA;border-left:4px solid {COR["nao"]};padding:8px 12px;margin-bottom:16px"><b>Gatilhos disparados</b><ul style="margin:6px 0 0;padding-left:18px">{itens}</ul></div>')
    if gerais:
        itens = "".join(
            f'<li style="margin-bottom:5px"><a href="{_e(n.get("url"))}" style="color:#222;text-decoration:none">{_e(n["titulo"])}</a> <span style="color:#888;font-size:12px">{_e(n.get("fonte") or "")}</span></li>'
            for n in gerais)
        partes.append(f'<h3 style="font-size:13px;color:#666;font-weight:600;margin:18px 0 6px">Notícias do dia</h3><ul style="margin:0 0 16px;padding-left:18px">{itens}</ul>')
    partes.append('<h3 style="font-size:13px;color:#666;font-weight:600;margin:18px 0 6px">Por posição</h3>')
    for p in briefing.get("posicoes", []):
        s = p.get("saida")
        ativo = _e(p.get("ativo"))
        if not s:
            partes.append(f'<div style="border-top:1px solid #ddd;padding:10px 0"><b>{ativo}</b> <span style="color:#666">— briefing indisponível ({_e(p.get("erro") or "sem saída válida")})</span></div>')
            continue
        citadas = _noticias_citadas(p, urls)
        links = {n["id"]: n.get("url") for n in citadas}
        v = s.get("tese_continua", "indisponivel")
        bloco = [f'<div style="border-top:1px solid #ddd;padding:12px 0">',
                 f'<div style="margin-bottom:6px"><b style="font-size:16px">{ativo}</b> <span style="display:inline-block;margin-left:8px;padding:1px 8px;border-radius:3px;font-size:12px;color:#fff;background:{COR.get(v, "#666")}">{_e(ROTULO.get(v, v))}</span></div>',
                 f'<p style="{css_p}">{_refs(s.get("situacao", ""), links)}</p>',
                 f'<p style="{css_p}">{_refs(s.get("justificativa", ""), links)}</p>']
        if s.get("leitura_fundamentos"):
            bloco.append(f'<p style="{css_p};color:#444"><span style="color:#888">Fundamentos.</span> {_refs(s["leitura_fundamentos"], links)}</p>')
        if s.get("pontos_de_atencao"):
            bloco.append('<ul style="margin:0 0 8px;padding-left:18px">' + "".join(f'<li>{_refs(x, links)}</li>' for x in s["pontos_de_atencao"]) + "</ul>")
        if citadas:
            bloco.append('<div style="font-size:12px;color:#666">' + "<br>".join(
                f'{_e(n["id"])} · ' + (f'<a href="{_e(n["url"])}" style="color:#444">{_e(n["titulo"])}</a>' if n.get("url") else _e(n["titulo"])) + f' <span style="color:#999">{_e(n.get("fonte") or "")}</span>'
                for n in citadas) + "</div>")
        bloco.append("</div>")
        partes.append("".join(bloco))
    partes.append(f'<p style="margin-top:20px;font-size:11px;color:#999">IA: {briefing.get("tokens_in", 0)} tokens in, {briefing.get("tokens_out", 0)} out, US$ {briefing.get("custo_usd", 0):.4f}. '
                  'A mesa descreve e checa a tese; não recomenda comprar nem vender.</p></div>')
    return "\n".join(partes)
