"""Notícias: coleta RSS (Google News por termo de busca de cada posição + feeds gerais), dedupe e
casamento com os ativos da carteira por regras explícitas.

Casar por substring é o erro clássico: "Vale a pena investir em CDB?" não é sobre a Vale. Aqui o
ticker no título vale 1.0; o nome (todos os tokens relevantes do termo) vale 0.8; frases de
exclusão por ativo derrubam o casamento por nome (nunca o por ticker). Cada notícia guarda só
título, fonte, data e URL — nada de scraping do corpo.
"""
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from mesa.armazenamento import Db, agora_utc
from mesa.carteira import Posicao

GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
FEEDS_GERAIS = {"InfoMoney": "https://www.infomoney.com.br/feed/",
                "Valor Investe": "https://valorinveste.globo.com/rss/valorinveste/",
                "Reuters Brasil": "https://news.google.com/rss/search?q=site:reuters.com+mercado+brasil&hl=pt-BR&gl=BR&ceid=BR:pt-419"}
UA = {"User-Agent": "Mozilla/5.0 (mesa)"}
# frases que indicam que o nome apareceu em outro sentido (só derrubam casamento por nome)
EXCLUSOES = {"VALE3": ["vale a pena", "vale mais", "vale do", "vale-tudo", "vale tudo", "não vale", "nao vale", "vale ouro"],
             "OIBR3": ["oi,", "oi!", "oi "], "MULT3": ["multi"], "ITSA4": ["itsa "], "AZUL4": ["azul-claro", "cor azul"]}
SUFIXOS_FUNDO = re.compile(r"\b(FIC|FIM|FIF|FIA|FI|FUNDO|DE|INVESTIMENTO|EM|COTAS|MULTIMERCADO|FINANCEIRO|RESPONSABILIDADE|LIMITADA|CLASSE|CIC|RL|LTDA)\b", re.I)
STOP = {"de", "da", "do", "das", "dos", "e", "em", "a", "o", "sa", "s/a", "ltda", "on", "pn", "nm", "n1", "n2"}


def sem_acento(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def normalizar_titulo(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", sem_acento(t).lower()).strip()


def url_canonica(url: str) -> str:
    p = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_") and k not in ("fbclid", "gclid")]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path, urlencode(q), ""))


def termos(p: Posicao) -> list[str]:
    if getattr(p, "busca", ""):
        return [t.strip() for t in p.busca.split(";") if t.strip()]
    if p.mercado in ("B3", "US"):
        return [p.identificador]
    if p.tipo == "fundo":
        nome = SUFIXOS_FUNDO.sub(" ", p.ativo)
        nome = re.sub(r"\s+", " ", nome).strip()
        return [nome] if nome else [p.ativo]
    return [p.identificador]


def _data(entry) -> str | None:
    for chave in ("published", "updated"):
        v = entry.get(chave)
        if v:
            try:
                return parsedate_to_datetime(v).astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError):
                try:
                    return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
                except ValueError:
                    continue
    return None


def _parse(conteudo, termo: str, fonte_padrao: str | None = None) -> list[dict]:
    import feedparser
    f = feedparser.parse(conteudo)
    itens = []
    for e in f.entries:
        titulo = (e.get("title") or "").strip()
        link = e.get("link") or ""
        if not titulo or not link:
            continue
        fonte = (e.get("source") or {}).get("title") or fonte_padrao or ""
        # Google News poe " - Fonte" no fim do titulo
        if fonte and titulo.endswith(f" - {fonte}"):
            titulo = titulo[: -len(fonte) - 3].strip()
        itens.append({"url": url_canonica(link), "titulo": titulo, "fonte": fonte, "publicada_em": _data(e), "termo": termo})
    return itens


def coletar_rss(termos_busca: list[str], fetch=None, max_por_termo: int = 25, feeds_gerais: dict | None = FEEDS_GERAIS) -> list[dict]:
    """`fetch(url) -> bytes`; injetável nos testes. Falha num feed não derruba os outros."""
    if fetch is None:
        import requests

        def fetch(url):
            return requests.get(url, headers=UA, timeout=30).content
    itens = []
    for termo in termos_busca:
        q = quote(f'"{termo}"' if " " in termo else termo)
        try:
            itens += _parse(fetch(GOOGLE_NEWS.format(q=q)), termo)[:max_por_termo]
        except Exception:  # noqa: BLE001 - um termo fora nao impede os outros
            continue
    for nome, url in (feeds_gerais or {}).items():
        try:
            itens += _parse(fetch(url), "", fonte_padrao=nome)[:max_por_termo]
        except Exception:  # noqa: BLE001
            continue
    return itens


def _tokens_relevantes(termo: str) -> list[str]:
    return [t for t in normalizar_titulo(termo).split() if len(t) >= 3 and t not in STOP]


def casar(titulo: str, posicoes: list[Posicao], exclusoes: dict | None = None) -> list[tuple[str, str, float]]:
    exclusoes = EXCLUSOES if exclusoes is None else exclusoes
    tn = normalizar_titulo(titulo)
    palavras = set(tn.split())
    out = []
    for p in posicoes:
        ident = p.identificador
        ticker = ident if p.mercado in ("B3", "US") else None
        # ticker so casa em MAIUSCULAS no titulo original: "VOO" e um ETF, "voo" e um helicoptero
        if ticker and re.search(rf"(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])", sem_acento(titulo)):
            out.append((ident, "ticker", 1.0))
            continue
        excluida = any(frase in titulo.lower() for frase in exclusoes.get(ident, []))
        if excluida:
            continue
        for termo in termos(p):
            toks = _tokens_relevantes(termo)
            if ticker and normalizar_titulo(termo) == normalizar_titulo(ticker):
                continue  # o proprio ticker como termo so casa pelo caminho de maiusculas acima
            if toks and all(t in palavras for t in toks):
                out.append((ident, "nome", 0.8))
                break
    return out


def salvar(db: Db, itens: list[dict], posicoes: list[Posicao]) -> dict:
    novas = casadas = 0
    con = db.con
    vistos_titulos = {r[0] for r in con.execute("SELECT titulo_norm FROM noticias WHERE coletada_em >= ?",
                                                 ((agora_utc() - timedelta(days=3)).isoformat(),)).fetchall()}
    for it in itens:
        tn = normalizar_titulo(it["titulo"])
        if tn in vistos_titulos:
            # ja existe: nao duplica, mas (re)casa - a carteira pode ter ganhado termos desde a ultima coleta
            r = con.execute("SELECT id FROM noticias WHERE titulo_norm = ? ORDER BY id DESC LIMIT 1", (tn,)).fetchone()
            nid = r["id"] if r else None
        else:
            cur = con.execute("INSERT OR IGNORE INTO noticias (url, titulo, fonte, publicada_em, coletada_em, titulo_norm) VALUES (?,?,?,?,?,?)",
                              (it["url"], it["titulo"], it.get("fonte"), it.get("publicada_em"), agora_utc().isoformat(), tn))
            if cur.rowcount == 0:
                r = con.execute("SELECT id FROM noticias WHERE url = ?", (it["url"],)).fetchone()
                nid = r["id"] if r else None
            else:
                vistos_titulos.add(tn)
                novas += 1
                nid = cur.lastrowid
        if nid is None:
            continue
        for ident, metodo, score in casar(it["titulo"], posicoes):
            con.execute("INSERT OR IGNORE INTO noticia_ativo (noticia_id, ativo, metodo, score) VALUES (?,?,?,?)", (nid, ident, metodo, score))
            casadas += 1
    con.commit()
    return {"novas": novas, "casadas": casadas, "itens": len(itens)}


def recentes(db: Db, identificador: str, dias: int = 7, limite: int = 12) -> list[dict]:
    desde = (agora_utc() - timedelta(days=dias)).isoformat()
    rows = db.con.execute("""SELECT n.id, n.titulo, n.fonte, n.publicada_em, n.url, na.metodo, na.score
                             FROM noticias n JOIN noticia_ativo na ON na.noticia_id = n.id
                             WHERE na.ativo = ? AND coalesce(n.publicada_em, n.coletada_em) >= ?
                             ORDER BY coalesce(n.publicada_em, n.coletada_em) DESC LIMIT ?""", (identificador, desde, limite)).fetchall()
    return [dict(r) for r in rows]


def gerais(db: Db, dias: int = 2, limite: int = 15) -> list[dict]:
    desde = (agora_utc() - timedelta(days=dias)).isoformat()
    rows = db.con.execute("""SELECT id, titulo, fonte, publicada_em, url FROM noticias
                             WHERE coalesce(publicada_em, coletada_em) >= ? ORDER BY coalesce(publicada_em, coletada_em) DESC LIMIT ?""",
                          (desde, limite)).fetchall()
    return [dict(r) for r in rows]
