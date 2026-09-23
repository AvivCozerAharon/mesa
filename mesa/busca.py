"""Busca de ativos para o autocomplete: ticker (Yahoo), fundo por nome/CNPJ (cadastro CVM local) e
título do Tesouro (curvas locais).

Digitar "PETR4" ou um CNPJ de cabeça é o tipo de atrito que faz a carteira ficar desatualizada, e
carteira desatualizada invalida todo o resto (métricas, gatilhos, briefing). Por isso a busca devolve
tudo que a posição precisa — tipo, identificador, nome, mercado, moeda — e o preço vem de `cotacao`.

O cadastro de fundos e as curvas são locais (Parquet + DuckDB), então busca de fundo funciona offline.
Ticker depende do Yahoo; sem rede a lista fica só com o que é local e a UI diz isso.
"""
import os
import re
import time
import zipfile
from collections import OrderedDict
from datetime import date, timedelta

import pandas as pd

from mesa.armazenamento import Consulta, partes_fundo

URL_BUSCA = "https://query2.finance.yahoo.com/v1/finance/search"
CABECALHO = {"User-Agent": "Mozilla/5.0 (compatible; mesa/1.0)"}
BOLSAS_US = {"NYQ": "NYSE", "NMS": "Nasdaq", "NGM": "Nasdaq", "NCM": "Nasdaq", "ASE": "NYSE American", "PCX": "NYSE Arca", "BTS": "Cboe BZX"}
SO_DIGITOS = re.compile(r"\D")


def tipo_b3(ticker: str, quote_type: str) -> str:
    """Sufixo numérico da B3: 3/4/5/6 = ação, 11 = FII ou ETF, 31–39 = BDR."""
    n = re.sub(r"^[A-Z]+", "", ticker)
    if n in ("31", "32", "33", "34", "35", "36", "37", "38", "39"):
        return "bdr"
    if n == "11":
        return "etf" if quote_type == "ETF" else "fii"
    return "acao"


def _de_yahoo(q: dict) -> dict | None:
    simbolo, bolsa, qt = q.get("symbol") or "", q.get("exchange") or "", (q.get("quoteType") or "").upper()
    if qt not in ("EQUITY", "ETF", "MUTUALFUND"):
        return None
    nome = (q.get("longname") or q.get("shortname") or "").strip()
    if bolsa == "SAO" and simbolo.endswith(".SA"):
        ident = simbolo[:-3]
        return {"tipo": tipo_b3(ident, qt), "identificador": ident, "ativo": nome, "mercado": "B3", "moeda": "BRL",
                "onde": "B3", "codigo": ident}
    if bolsa in BOLSAS_US:
        return {"tipo": "etf_us" if qt == "ETF" else "acao_us", "identificador": simbolo, "ativo": nome,
                "mercado": "US", "moeda": "USD", "onde": BOLSAS_US[bolsa], "codigo": simbolo}
    return None


def parece_ticker(termo: str) -> bool:
    """CNPJ ou "tesouro ipca" não têm ticker: não vale pagar a ida ao Yahoo (até 8 s) por eles."""
    t = termo.strip()
    letras = sum(c.isalpha() for c in t)
    if letras < 2 or len(SO_DIGITOS.sub("", t)) >= 8:
        return False
    return not any(p in t.upper() for p in ("TESOURO", "IPCA+", "SELIC", "PREFIXADO", "RENDA+"))


_sessao = None
_cache: "OrderedDict[str, tuple[float, list[dict]]]" = OrderedDict()
CACHE_SEGUNDOS = 180
CACHE_MAX = 64


def _get_padrao(url, **kw):
    """Sessão reaproveitada: sem ela cada tecla paga um handshake TLS novo com o Yahoo."""
    global _sessao
    if _sessao is None:
        import requests
        _sessao = requests.Session()
        _sessao.headers.update(CABECALHO)
    return _sessao.get(url, timeout=5, **kw)


def tickers(termo: str, limite: int = 6, get=None) -> list[dict]:
    termo = termo.strip()
    if len(termo) < 2 or not parece_ticker(termo):
        return []
    chave = termo.upper()
    agora = time.monotonic()
    guardado = _cache.get(chave)
    if guardado and agora - guardado[0] < CACHE_SEGUNDOS:
        return guardado[1][:limite]
    r = (get or _get_padrao)(URL_BUSCA, params={"q": termo, "quotesCount": 12, "newsCount": 0, "listsCount": 0}, headers=CABECALHO)
    r.raise_for_status()
    out = []
    for q in r.json().get("quotes", []):
        item = _de_yahoo(q)
        if item and not any(x["identificador"] == item["identificador"] for x in out):
            out.append(item)
    _cache[chave] = (agora, out)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)
    return out[:limite]


def formatar_cnpj(cnpj: str) -> str:
    c = SO_DIGITOS.sub("", cnpj)
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}" if len(c) == 14 else cnpj


def fundos(consulta: Consulta, termo: str, limite: int = 8) -> list[dict]:
    """Nome ou CNPJ. Fundo grande primeiro: com 36 mil classes no cadastro, o PL é o melhor desempate."""
    digitos = SO_DIGITOS.sub("", termo)
    if len(digitos) >= 4 and len(digitos) >= len(termo.strip()) - 4:
        onde, param = "cnpj LIKE ?", [f"{digitos}%"]  # inclui as subclasses do mesmo CNPJ
    elif len(termo.strip()) < 3:
        return []
    else:
        palavras = [p for p in termo.upper().split() if len(p) > 1]
        if not palavras:
            return []
        onde = " AND ".join(["upper(nome) LIKE ?"] * len(palavras))
        param = [f"%{p}%" for p in palavras]
    tem_sub = "subclasse" in consulta.colunas("fundos_cadastro")
    col_sub = "coalesce(subclasse, '')" if tem_sub else "''"
    df = consulta._df(f"SELECT cnpj, {col_sub} AS subclasse, nome, gestor, classe, pl, situacao "
                      f"FROM fundos_cadastro WHERE {onde} ORDER BY coalesce(pl, 0) DESC, 2 LIMIT {int(limite)}", param)
    out = []
    for _, r in df.iterrows():
        pl = None if pd.isna(r["pl"]) else float(r["pl"])
        sub = r["subclasse"] or ""
        detalhe = " · ".join(x for x in [("subclasse" if sub else None), r["gestor"] or None, r["classe"] or None,
                                         f"PL R$ {pl / 1e6:,.0f} mi".replace(",", ".") if pl else None] if x)
        out.append({"tipo": "fundo", "identificador": f"{r['cnpj']}:{sub}" if sub else r["cnpj"],
                    "ativo": r["nome"], "mercado": "CVM", "moeda": "BRL", "onde": "CVM",
                    "codigo": formatar_cnpj(r["cnpj"]) + (f" · {sub}" if sub else ""), "detalhe": detalhe,
                    "inativo": (r["situacao"] or "").upper() not in ("EM FUNCIONAMENTO NORMAL", "")})
    return out


def tesouro(consulta: Consulta, termo: str, limite: int = 6) -> list[dict]:
    t = termo.strip().upper()
    if len(t) < 3 or not ("TESOURO" in t or "IPCA" in t or "SELIC" in t or "PREFIXADO" in t or "RENDA" in t or "EDUCA" in t or t.isdigit()):
        return []
    df = consulta._df("""SELECT titulo, vencimento, max(data) AS data FROM curvas WHERE pais = 'BR' AND pu IS NOT NULL
                         GROUP BY titulo, vencimento ORDER BY vencimento""", [])
    if df.empty:
        return []
    df["nome"] = df["titulo"] + " " + pd.to_datetime(df["vencimento"]).dt.year.astype(str)
    palavras = [p for p in t.replace("+", "+ ").split() if p]
    alvo = df[df["nome"].str.upper().apply(lambda n: all(p in n for p in palavras))]
    return [{"tipo": "tesouro", "identificador": r["nome"], "ativo": r["nome"], "mercado": "TD", "moeda": "BRL",
             "onde": "Tesouro Direto", "codigo": pd.Timestamp(r["vencimento"]).strftime("%d/%m/%Y"),
             "detalhe": f"vence em {pd.Timestamp(r['vencimento']).date().isoformat()}"}
            for _, r in alvo.head(limite).iterrows()]


def buscar(consulta: Consulta, termo: str, fontes: str = "todas", get=None) -> dict:
    """Uma fonte que falha não derruba a busca — a UI mostra o aviso.

    `fontes` existe porque o Yahoo leva ~400 ms e o que está no disco leva ~50 ms: a tela pede as duas
    coisas em paralelo, pinta o que é local na hora e encaixa os tickers quando chegam.
    """
    locais = [("tesouro", lambda: tesouro(consulta, termo)), ("fundos", lambda: fundos(consulta, termo))]
    remotas = [("tickers", lambda: tickers(termo, get=get))]
    escolhidas = locais if fontes == "locais" else remotas if fontes == "tickers" else locais + remotas
    itens, avisos = [], []
    for nome, fn in escolhidas:
        try:
            itens += fn()
        except Exception as e:  # noqa: BLE001 - Yahoo fora do ar nao pode travar a busca de fundo
            avisos.append(f"{nome}: {type(e).__name__}")
    return {"itens": itens, "avisos": avisos}


def cota_cvm(dados_dir: str, cnpj: str, quando: date, get=None) -> dict:
    """Cota de um fundo num dia passado, direto do informe diário da CVM.

    O job já guarda 36 zips mensais em `dados/cvm/raw`, então na maior parte das compras isto não
    baixa nada. Fundo novo na carteira ainda não tem série coletada — é justamente o caso de quem
    está lançando a posição agora.
    """
    from mesa.fontes.cvm import URL_INFORME, parse_informe
    cnpj, sub = partes_fundo(cnpj)
    cnpj = SO_DIGITOS.sub("", cnpj)
    pasta = os.path.join(dados_dir, "cvm", "raw")
    os.makedirs(pasta, exist_ok=True)
    partes = []
    for delta in (0, 1):  # o mes da compra e o anterior, para cair no pregao anterior quando o dia nao tem cota
        ref = (quando.replace(day=1) - timedelta(days=1)) if delta else quando
        caminho = os.path.join(pasta, f"inf_diario_fi_{ref:%Y%m}.zip")
        if not os.path.exists(caminho):
            if get is None:
                import requests
                get = lambda url: requests.get(url, timeout=90)  # noqa: E731
            r = get(URL_INFORME.format(aaaamm=f"{ref:%Y%m}"))
            r.raise_for_status()
            with open(caminho, "wb") as fh:
                fh.write(r.content)
        with zipfile.ZipFile(caminho) as zf:
            for nome in zf.namelist():
                if nome.lower().endswith(".csv"):
                    linhas = parse_informe(zf.read(nome), {cnpj})
                    if not linhas.empty and "subclasse" in linhas.columns:
                        linhas = linhas[linhas["subclasse"].fillna("") == sub]
                    partes.append(linhas)
        df = pd.concat([x for x in partes if not x.empty], ignore_index=True) if any(not x.empty for x in partes) else pd.DataFrame()
        if not df.empty:
            ate = df[pd.to_datetime(df["data"]).dt.date <= quando].sort_values("data")
            if not ate.empty:
                r = ate.iloc[-1]
                return {"preco": float(r["cota"]), "data": pd.Timestamp(r["data"]).date().isoformat(), "fonte": "informe diário da CVM"}
    return {"preco": None, "erro": f"a CVM não publicou cota deste fundo até {quando.isoformat()}"}


def preco_em(consulta: Consulta, tipo: str, identificador: str, quando: date, dados_dir: str = "dados", historico_fn=None) -> dict:
    """Preço numa data passada, para lançar uma compra antiga pelo valor investido."""
    if tipo == "fundo":
        df = consulta.cotas([identificador])
        if not df.empty:
            ate = df[pd.to_datetime(df["data"]).dt.date <= quando].sort_values("data")
            if not ate.empty:
                r = ate.iloc[-1]
                return {"preco": float(r["cota"]), "data": pd.Timestamp(r["data"]).date().isoformat(), "fonte": "CVM"}
        return cota_cvm(dados_dir, identificador, quando)
    if tipo == "tesouro":
        df = consulta._df("""SELECT data, pu FROM curvas WHERE pais = 'BR' AND pu IS NOT NULL
                             AND titulo || ' ' || strftime(vencimento, '%Y') = ? AND data <= ? ORDER BY data DESC LIMIT 1""",
                          [identificador, quando.isoformat()])
        if not df.empty:
            return {"preco": float(df.iloc[0]["pu"]), "data": pd.Timestamp(df.iloc[0]["data"]).date().isoformat(), "fonte": "Tesouro Direto"}
        return {"preco": None, "erro": f"sem PU deste título em {quando.isoformat()}"}
    mercado = "B3" if tipo in ("acao", "fii", "etf", "bdr") else "US"
    df = consulta.precos([identificador])
    if not df.empty:
        ate = df[pd.to_datetime(df["data"]).dt.date <= quando].sort_values("data")
        if not ate.empty:
            r = ate.iloc[-1]
            return {"preco": float(r["fechamento"]), "data": pd.Timestamp(r["data"]).date().isoformat(), "fonte": "fechamento guardado"}
    if historico_fn is None:
        from mesa.fontes.yahoo import fechamento_em as historico_fn
    r = historico_fn(identificador, mercado, quando)
    return r or {"preco": None, "erro": f"sem fechamento em {quando.isoformat()} — confira a data"}


def cotacao(consulta: Consulta, tipo: str, identificador: str, cotacao_fn=None) -> dict:
    """Preço de hoje para preencher o campo de preço médio na hora de lançar a posição."""
    if tipo == "fundo":
        df = consulta.cotas([identificador])
        if not df.empty:
            r = df.sort_values("data").iloc[-1]
            return {"preco": float(r["cota"]), "data": pd.Timestamp(r["data"]).date().isoformat(), "fonte": "CVM"}
        return {"preco": None, "erro": "fundo sem cota coletada ainda — roda depois do próximo job da manhã"}
    if tipo == "tesouro":
        df = consulta._df("""SELECT data, pu, taxa FROM curvas WHERE pais = 'BR' AND pu IS NOT NULL
                             AND titulo || ' ' || strftime(vencimento, '%Y') = ? ORDER BY data DESC LIMIT 1""", [identificador])
        if not df.empty:
            r = df.iloc[0]
            return {"preco": float(r["pu"]), "data": pd.Timestamp(r["data"]).date().isoformat(), "fonte": "Tesouro Direto",
                    "detalhe": f"taxa {float(r['taxa']):.2f}%"}
        return {"preco": None, "erro": "título sem PU na curva local"}
    mercado = "B3" if tipo in ("acao", "fii", "etf", "bdr") else "US"
    if cotacao_fn is None:
        from mesa.fontes.yahoo import cotacao_atual as cotacao_fn
    q = cotacao_fn([(identificador, mercado)]).get(identificador)
    if q:
        return {"preco": q["preco"], "data": None, "fonte": "Yahoo (atrasado ~15 min)", "moeda": q.get("moeda")}
    df = consulta.precos([identificador])
    if not df.empty:
        r = df.sort_values("data").iloc[-1]
        return {"preco": float(r["fechamento"]), "data": pd.Timestamp(r["data"]).date().isoformat(), "fonte": "último fechamento"}
    return {"preco": None, "erro": "sem cotação — confira o código do ativo"}
