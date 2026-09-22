"""Gatilhos: regras declarativas que o usuário configura por posição, avaliadas sobre as métricas do
dia e as notícias recentes. Idempotentes: um disparo é (gatilho, hash do estado); a mesma condição
não avisa de novo enquanto o estado não mudar — e volta a avisar se sair e voltar.

Gatilho é o único lugar onde o sistema "opina": e mesmo assim só diz "a condição que VOCÊ definiu
aconteceu". O que fazer com isso continua sendo decisão do usuário.
"""
import hashlib
import json
from datetime import date

from mesa.armazenamento import Db, agora_utc


def _g(m: dict, *chaves):
    for k in chaves:
        if not isinstance(m, dict) or k not in m or m[k] is None:
            return None
        m = m[k]
    return m


def _pct(x):
    return None if x is None else round(float(x), 1)


def queda_desde_compra(m, p, _n):
    v = _g(m, "vs_preco_medio_pct")
    lim = p.get("pct", 15)
    return {"queda_pct": _pct(v), "limite": lim} if v is not None and v <= -lim else None


def queda_1m(m, p, _n):
    v = _g(m, "retornos", "1m")
    lim = p.get("pct", 10)
    return {"queda_1m_pct": _pct(v), "limite": lim} if v is not None and v <= -lim else None


def abaixo_mm50(m, p, _n):
    u, mm = _g(m, "ultimo"), _g(m, "mm50")
    return {"ultimo": _pct(u), "mm50": _pct(mm)} if u is not None and mm is not None and u < mm else None


def abaixo_mm200(m, p, _n):
    u, mm = _g(m, "ultimo"), _g(m, "mm200")
    return {"ultimo": _pct(u), "mm200": _pct(mm)} if u is not None and mm is not None and u < mm else None


def min_52s(m, p, _n):
    v = _g(m, "s52", "pct_do_fundo")
    tol = p.get("tolerancia_pct", 1)
    return {"acima_do_fundo_pct": _pct(v)} if v is not None and v <= tol else None


def max_52s(m, p, _n):
    v = _g(m, "s52", "pct_do_topo")
    tol = p.get("tolerancia_pct", 1)
    return {"abaixo_do_topo_pct": _pct(v)} if v is not None and v >= -tol else None


def vol_spike(m, p, _n):
    v30, v252 = _g(m, "vol_30d"), _g(m, "vol_252d")
    f = p.get("fator", 2)
    return {"vol_30d": _pct(v30), "vol_252d": _pct(v252), "fator": f} if v30 and v252 and v30 >= f * v252 else None


def drawdown(m, p, _n):
    v = _g(m, "drawdown", "atual")
    lim = p.get("pct", 20)
    return {"drawdown_pct": _pct(v), "limite": lim} if v is not None and v <= -lim else None


def noticia_contem(m, p, noticias):
    termos = [t.lower() for t in p.get("termos", [])]
    achadas = [n for n in noticias if any(t in (n.get("titulo") or "").lower() for t in termos)]
    if not achadas:
        return None
    return {"noticias": [{"id": n["id"], "titulo": n["titulo"]} for n in achadas[:3]], "termos": termos}


def fundo_abaixo_bench(m, p, _n):
    v = _g(m, "janelas_12m", "pct")
    lim = p.get("pct", 50)
    return {"pct_janelas_batidas": _pct(v), "limite": lim} if v is not None and v < lim else None


def fundo_pct_bench_12m(m, p, _n):
    v = _g(m, "pct_do_bench", "12m")
    lim = p.get("pct", 90)
    return {"pct_do_bench_12m": _pct(v), "limite": lim} if v is not None and v < lim else None


def fundo_resgate(m, p, _n):
    capt, pl = _g(m, "captacao_liquida_6m"), _g(m, "pl")
    lim = p.get("pct", 20)
    if capt is None or not pl:
        return None
    razao = capt / pl * 100
    return {"captacao_liquida_6m_pct_pl": _pct(razao), "limite": lim} if razao <= -lim else None


REGRAS = {f.__name__: f for f in (queda_desde_compra, queda_1m, abaixo_mm50, abaixo_mm200, min_52s, max_52s, vol_spike,
                                  drawdown, noticia_contem, fundo_abaixo_bench, fundo_pct_bench_12m, fundo_resgate)}
PADRAO = [("queda_desde_compra", {"pct": 15}), ("min_52s", {}), ("vol_spike", {}),
          ("noticia_contem", {"termos": ["recuperação judicial", "fraude", "investiga", "CVM abre processo", "delisting"]}),
          ("fundo_abaixo_bench", {}), ("fundo_resgate", {})]
DESCRICOES = {"queda_desde_compra": "caiu {limite} % ou mais desde a compra", "queda_1m": "caiu {limite} % ou mais em 1 mês",
              "abaixo_mm50": "abaixo da média de 50 dias", "abaixo_mm200": "abaixo da média de 200 dias",
              "min_52s": "na mínima de 52 semanas", "max_52s": "na máxima de 52 semanas",
              "vol_spike": "volatilidade de 30 d ≥ {fator}× a de 12 m", "drawdown": "drawdown de {limite} % ou mais",
              "noticia_contem": "notícia com termo sensível", "fundo_abaixo_bench": "fundo bateu o benchmark em menos de {limite} % das janelas de 12 m",
              "fundo_pct_bench_12m": "fundo abaixo de {limite} % do benchmark em 12 m", "fundo_resgate": "resgates de {limite} % do PL em 6 m"}


def garantir_padrao(db: Db, posicao_id: int) -> int:
    """Cria os gatilhos padrão de uma posição uma única vez (o usuário pode apagar depois)."""
    marca = db.con.execute("SELECT 1 FROM gatilhos WHERE posicao_id = ? AND regra = '__padrao__'", (posicao_id,)).fetchone()
    if marca:
        return 0
    ts = agora_utc().isoformat()
    for regra, params in PADRAO:
        db.con.execute("INSERT INTO gatilhos (posicao_id, regra, parametros, ativo, criado_em) VALUES (?,?,?,1,?)",
                       (posicao_id, regra, json.dumps(params, ensure_ascii=False), ts))
    db.con.execute("INSERT INTO gatilhos (posicao_id, regra, parametros, ativo, criado_em) VALUES (?,'__padrao__','{}',0,?)", (posicao_id, ts))
    db.con.commit()
    return len(PADRAO)


def listar(db: Db, posicao_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM gatilhos WHERE ativo = 1 AND regra != '__padrao__'"
    args: tuple = ()
    if posicao_id is not None:
        sql += " AND (posicao_id = ? OR posicao_id IS NULL)"
        args = (posicao_id,)
    return [{**dict(r), "parametros": json.loads(r["parametros"])} for r in db.con.execute(sql + " ORDER BY id", args).fetchall()]


def criar(db: Db, posicao_id: int | None, regra: str, parametros: dict) -> int:
    if regra not in REGRAS:
        raise ValueError(f"regra desconhecida: {regra} (use {', '.join(REGRAS)})")
    cur = db.con.execute("INSERT INTO gatilhos (posicao_id, regra, parametros, ativo, criado_em) VALUES (?,?,?,1,?)",
                         (posicao_id, regra, json.dumps(parametros, ensure_ascii=False), agora_utc().isoformat()))
    db.con.commit()
    return cur.lastrowid


def excluir(db: Db, gatilho_id: int) -> None:
    db.con.execute("UPDATE gatilhos SET ativo = 0 WHERE id = ?", (gatilho_id,))
    db.con.commit()


def _hash(regra: str, params: dict, detalhe: dict) -> str:
    return hashlib.sha1(json.dumps([regra, params, detalhe], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def avaliar(db: Db, posicao_id: int, metricas: dict | None, noticias: list[dict], hoje: date) -> list[dict]:
    """Devolve só os disparos NOVOS de hoje para a posição."""
    metricas = metricas or {}
    novos = []
    for g in listar(db, posicao_id):
        fn = REGRAS.get(g["regra"])
        if fn is None:
            continue
        try:
            detalhe = fn(metricas, g["parametros"], noticias)
        except Exception as e:  # noqa: BLE001 - regra com dado faltando nao derruba as outras
            detalhe = None
            db.registrar_coleta(f"gatilho:{g['regra']}", hoje, None, 0, f"{type(e).__name__}: {e}")
        if not detalhe:
            continue
        h = _hash(g["regra"], g["parametros"], detalhe)
        cur = db.con.execute("INSERT OR IGNORE INTO disparos (gatilho_id, posicao_id, data, estado_hash, detalhe, visto) VALUES (?,?,?,?,?,0)",
                             (g["id"], posicao_id, hoje.isoformat(), h, json.dumps(detalhe, ensure_ascii=False)))
        if cur.rowcount:
            novos.append({"id": cur.lastrowid, "gatilho_id": g["id"], "posicao_id": posicao_id, "regra": g["regra"],
                          "parametros": g["parametros"], "detalhe": detalhe, "data": hoje.isoformat(),
                          "descricao": DESCRICOES.get(g["regra"], g["regra"]).format(**{**g["parametros"], **detalhe})})
    db.con.commit()
    return novos


def pendentes(db: Db, incluir_vistos: bool = False) -> list[dict]:
    sql = """SELECT d.*, g.regra, g.parametros, p.ativo FROM disparos d JOIN gatilhos g ON g.id = d.gatilho_id
             LEFT JOIN posicoes p ON p.id = d.posicao_id"""
    if not incluir_vistos:
        sql += " WHERE d.visto = 0"
    rows = db.con.execute(sql + " ORDER BY d.id DESC LIMIT 100").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["parametros"], d["detalhe"] = json.loads(d["parametros"]), json.loads(d["detalhe"])
        d["descricao"] = DESCRICOES.get(d["regra"], d["regra"]).format(**{**d["parametros"], **d["detalhe"]})
        out.append(d)
    return out


def marcar_visto(db: Db, disparo_id: int) -> None:
    db.con.execute("UPDATE disparos SET visto = 1 WHERE id = ?", (disparo_id,))
    db.con.commit()


def do_dia(db: Db, hoje: date) -> list[dict]:
    """Todos os disparos registrados hoje (novos ou nao): e o que entra no briefing, para que
    reexecutar o job no mesmo dia gere a mesma entrada (e nao chame a IA de novo)."""
    rows = db.con.execute("""SELECT d.*, g.regra, g.parametros, p.ativo FROM disparos d JOIN gatilhos g ON g.id = d.gatilho_id
                             LEFT JOIN posicoes p ON p.id = d.posicao_id WHERE d.data = ? ORDER BY d.id""", (hoje.isoformat(),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["parametros"], d["detalhe"] = json.loads(d["parametros"]), json.loads(d["detalhe"])
        d["descricao"] = DESCRICOES.get(d["regra"], d["regra"]).format(**{**d["parametros"], **d["detalhe"]})
        out.append(d)
    return out
