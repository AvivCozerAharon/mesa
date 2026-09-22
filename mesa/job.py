"""Jobs: coleta (manhã e fechamento) e cálculo das métricas da carteira.

Cada etapa é idempotente por (fonte, data): rodar duas vezes no mesmo dia sobrescreve o mesmo
Parquet. Uma fonte com erro é registrada em `coletas` e as outras seguem — nunca derruba o job.
"""
import logging
from datetime import date, datetime

import pandas as pd

from mesa.armazenamento import Consulta, Db, gravar_parquet
from mesa.calendario import agora_brt
from mesa.carteira import Carteira, resumo, valorizar
from mesa.config import Config
from mesa.fontes import Contexto, executar
from mesa.fontes.bcb import SGS
from mesa.fontes.cvm import Cadastro, InformeDiario, Lamina
from mesa.fontes.tesouro import TesouroDireto
from mesa.fontes.treasury_us import Treasury
from mesa.fontes.yahoo import Yahoo
from mesa import email as email_mod
from mesa import gatilhos as gat
from mesa import ia
from mesa import noticias as noti
from mesa.metricas import indice_cdi, metricas_fundo, metricas_posicao

log = logging.getLogger("mesa.job")
BENCH_POR_TIPO = {"acao": "IBOV", "fii": "IFIX", "etf": "IBOV", "bdr": "SP500", "acao_us": "SP500", "etf_us": "SP500",
                  "fundo": "CDI", "tesouro": "CDI"}


def contexto(cfg: Config, carteira: Carteira, agora: datetime | None = None) -> Contexto:
    return Contexto(agora=agora or agora_brt(), tickers=carteira.tickers(), cnpjs=carteira.cnpjs(),
                    dados_dir=cfg.dados_dir, anos_precos=cfg.anos_precos, meses_cvm=cfg.meses_cvm)


def coletar(cfg: Config, db: Db, fontes: list, ctx: Contexto) -> list[dict]:
    resultados = []
    for fonte in fontes:
        df, col = executar(fonte, ctx)
        if col.ok and not df.empty:
            gravar_parquet(cfg.dados_dir, fonte.tabela, ctx.agora.date(), df, fonte=fonte.nome)
        db.registrar_coleta(fonte.nome, ctx.agora.date(), col.linhas if col.ok else None, col.duracao_ms, col.erro)
        log.info("coleta %s ok=%s linhas=%s %sms %s", fonte.nome, col.ok, col.linhas, col.duracao_ms, col.erro or col.detalhe)
        resultados.append({"fonte": fonte.nome, "ok": col.ok, "linhas": col.linhas, "erro": col.erro, "detalhe": col.detalhe})
    return resultados


def fontes_manha(cfg: Config, consulta: Consulta) -> list:
    def cadastro_fn():
        cad = consulta.cadastro_fundos()
        return cad if not cad.empty else None
    return [Cadastro(), Lamina(), InformeDiario(cadastro_fn), SGS(), TesouroDireto(), Treasury()]


def fontes_fechamento() -> list:
    return [Yahoo()]


def _serie(df: pd.DataFrame, chave: str, valor: str, coluna: str, col_chave: str = "ativo") -> pd.Series:
    sub = df[df[col_chave] == valor]
    if sub.empty:
        return pd.Series(dtype=float)
    return pd.Series(sub[coluna].values, index=pd.to_datetime(sub["data"]))


def benchmarks(consulta: Consulta) -> dict[str, pd.Series]:
    """Séries de benchmark como 'preço': índices do Yahoo direto; CDI vira índice acumulado."""
    out = {}
    precos = consulta.precos(["IBOV", "SP500", "IFIX", "USDBRL", "NASDAQ"])
    for nome in ("IBOV", "SP500", "IFIX", "USDBRL", "NASDAQ"):
        out[nome] = _serie(precos, "ativo", nome, "fechamento") if not precos.empty else pd.Series(dtype=float)
    cdi = consulta.benchmark("CDI")
    out["CDI"] = indice_cdi(cdi) if not cdi.empty else pd.Series(dtype=float)
    out["CDI_diario"] = cdi
    return out


def cambio_atual(consulta: Consulta) -> tuple[float | None, date | None]:
    s = consulta.precos(["USDBRL"])
    if s.empty:
        return None, None
    ult = s.sort_values("data").iloc[-1]
    return float(ult["fechamento"]), pd.Timestamp(ult["data"]).date()


def precos_atuais(consulta: Consulta, carteira: Carteira) -> dict[str, tuple[float, date]]:
    out = {}
    pos = carteira.listar()
    tick = [p.identificador for p in pos if p.mercado in ("B3", "US")]
    if tick:
        df = consulta.precos(tick)
        if not df.empty:
            for ident, sub in df.groupby("ativo"):
                ult = sub.sort_values("data").iloc[-1]
                out[ident] = (float(ult["fechamento"]), pd.Timestamp(ult["data"]).date())
    cnpjs = carteira.cnpjs()
    if cnpjs:
        df = consulta.cotas(cnpjs)
        if not df.empty:
            for cnpj, sub in df.groupby("cnpj"):
                ult = sub.sort_values("data").iloc[-1]
                out[cnpj] = (float(ult["cota"]), pd.Timestamp(ult["data"]).date())
    tesouro = [p.identificador for p in pos if p.tipo == "tesouro"]
    if tesouro:
        curvas = consulta._df("SELECT titulo, vencimento, data, pu FROM curvas WHERE pais = 'BR' AND pu IS NOT NULL ORDER BY data", [])
        if not curvas.empty:
            curvas["nome"] = curvas["titulo"] + " " + pd.to_datetime(curvas["vencimento"]).dt.year.astype(str)
            for nome in tesouro:
                sub = curvas[curvas["nome"] == nome]
                if not sub.empty:
                    ult = sub.iloc[-1]
                    out[nome] = (float(ult["pu"]), pd.Timestamp(ult["data"]).date())
    return out


def calcular(cfg: Config, db: Db, consulta: Consulta, carteira: Carteira, agora: datetime | None = None) -> dict:
    agora = agora or agora_brt()
    hoje = agora.date()
    bench = benchmarks(consulta)
    pos = carteira.listar()
    precos_df = consulta.precos([p.identificador for p in pos if p.mercado in ("B3", "US")])
    cadastro = consulta.cadastro_fundos(carteira.cnpjs()) if carteira.cnpjs() else pd.DataFrame()
    taxas = consulta.taxas_fundos(carteira.cnpjs()) if carteira.cnpjs() else pd.DataFrame()
    cotas = consulta.cotas(carteira.cnpjs()) if carteira.cnpjs() else pd.DataFrame()
    calculadas = 0
    for p in pos:
        b = bench.get(BENCH_POR_TIPO[p.tipo], pd.Series(dtype=float))
        if p.mercado in ("B3", "US") and not precos_df.empty:
            s = _serie(precos_df, "ativo", p.identificador, "ajustado")
            m = metricas_posicao(s, b, hoje, p.data_compra, p.preco_medio)
        elif p.tipo == "fundo" and not cotas.empty:
            cota = _serie(cotas, "cnpj", p.identificador, "cota", "cnpj")
            pl = _serie(cotas, "cnpj", p.identificador, "pl", "cnpj")
            capt = _serie(cotas, "cnpj", p.identificador, "captacao", "cnpj") - _serie(cotas, "cnpj", p.identificador, "resgate", "cnpj")
            info = cadastro[cadastro["cnpj"] == p.identificador].iloc[0] if not cadastro.empty and (cadastro["cnpj"] == p.identificador).any() else None
            pares = pares_do_fundo(consulta, p.identificador, info)
            tx = taxas[taxas["cnpj"] == p.identificador].iloc[0] if not taxas.empty and (taxas["cnpj"] == p.identificador).any() else None
            bench_decl = (tx["benchmark_declarado"] if tx is not None else "") or ""
            if "IBOV" in bench_decl.upper():
                b = bench.get("IBOV", b)
            m = metricas_fundo(cota, b, pares, hoje, None if tx is None else _f(tx["taxa_adm"]),
                               None if tx is None else _f(tx["taxa_perf"]), pl, capt, p.data_compra)
            m["benchmark_declarado"] = bench_decl
            if info is not None:
                m["classe"] = info["classe"]
                m["gestor"] = info["gestor"]
        else:
            m = {}
        if m:
            m.setdefault("benchmark", "IBOV" if m.get("benchmark_declarado", "").upper().find("IBOV") >= 0 else BENCH_POR_TIPO[p.tipo])
            db.gravar_metricas(hoje, p.id, m)
            calculadas += 1
    fx, _ = cambio_atual(consulta)
    val = valorizar(pos, precos_atuais(consulta, carteira), fx, hoje)
    res = resumo(val)
    log.info("metricas calculadas para %d/%d posicoes; carteira R$ %.0f", calculadas, len(pos), res["valor_brl"])
    return {"posicoes": len(pos), "calculadas": calculadas, "valor_brl": res["valor_brl"]}


def _f(x) -> float | None:
    try:
        return None if pd.isna(x) else float(x)
    except (TypeError, ValueError):
        return None


def pares_do_fundo(consulta: Consulta, cnpj: str, info) -> pd.DataFrame | None:
    if info is None or not info["classe"]:
        return None
    cad = consulta.cadastro_fundos()
    if cad.empty:
        return None
    cnpjs = cad[(cad["classe"] == info["classe"]) & (cad["cnpj"] != cnpj)]["cnpj"].tolist()
    if not cnpjs:
        return None
    cotas = consulta.cotas(cnpjs)
    if cotas.empty:
        return None
    tabela = cotas.pivot_table(index="data", columns="cnpj", values="cota")
    tabela.index = pd.to_datetime(tabela.index)
    # fundos publicam a cota com atraso diferente (D+1, D+2): sem ffill a ultima linha e quase toda NaN
    return tabela.sort_index().ffill()


def _etapa(db: Db, nome: str, hoje: date, fn) -> dict:
    """Roda uma etapa do job registrando sucesso/erro em `coletas` (a mesma tabela da operacao)."""
    import time
    inicio = time.time()
    try:
        res = fn()
        db.registrar_coleta(nome, hoje, res.get("n") if isinstance(res, dict) else None, int((time.time() - inicio) * 1000))
        return {"ok": True, **(res if isinstance(res, dict) else {})}
    except Exception as e:  # noqa: BLE001 - etapa com erro nao derruba as seguintes
        log.exception("etapa %s falhou", nome)
        db.registrar_coleta(nome, hoje, None, int((time.time() - inicio) * 1000), f"{type(e).__name__}: {e}"[:500])
        return {"ok": False, "erro": str(e)}


def coletar_noticias(db: Db, carteira: Carteira) -> dict:
    pos = carteira.listar()
    termos = sorted({t for p in pos for t in noti.termos(p)})
    itens = noti.coletar_rss(termos)
    r = noti.salvar(db, itens, pos)
    return {"n": r["novas"], **r, "termos": len(termos)}


def avaliar_gatilhos(db: Db, carteira: Carteira, hoje: date) -> dict:
    metricas = db.metricas_recentes()
    novos = []
    for p in carteira.listar():
        gat.garantir_padrao(db, p.id)
        for d in gat.avaliar(db, p.id, metricas.get(p.id), noti.recentes(db, p.identificador, dias=2), hoje):
            novos.append({**d, "ativo": p.ativo})
    return {"n": len(novos), "novos": novos}


def gerar_briefings(cfg: Config, db: Db, consulta: Consulta, carteira: Carteira, hoje: date, novos_disparos: list[dict], cliente=None) -> dict:
    if cliente is None:
        cliente = ia.ClienteOpenAI(cfg)  # levanta IAIndisponivel sem chave -> etapa registra erro
    metricas = db.metricas_recentes()
    por_pos = {}
    for d in novos_disparos:
        por_pos.setdefault(d["posicao_id"], []).append(d)
    resultados = []
    for p in carteira.listar()[: cfg.ia_max_posicoes_dia]:
        tese = carteira.tese(p.id)
        hist = carteira.historico_teses(p.id)
        entrada = ia.montar_entrada({**p.para_dict(), "tese_em": hist[-1]["criada_em"][:10] if hist else None},
                                    tese, metricas.get(p.id), noti.recentes(db, p.identificador), por_pos.get(p.id, []), hoje)
        r = ia.gerar_briefing_posicao(cliente, db, cfg, p.para_dict(), entrada, hoje)
        resultados.append({"ativo": p.ativo, "valido": r["valido"], "tese_continua": (r.get("saida") or {}).get("tese_continua"),
                           "reaproveitado": r.get("reaproveitado", False), "erro": r.get("erro")})
    fx, _ = cambio_atual(consulta)
    val = valorizar(carteira.listar(), precos_atuais(consulta, carteira), fx, hoje)
    res = resumo(val)
    pesos = [{"ativo": row["ativo"], "peso_pct": round(row["peso"], 1) if row.get("peso") is not None and row["peso"] == row["peso"] else None,
              "pnl_pct": round(row["pnl_pct"], 1) if row.get("pnl_pct") is not None and row["pnl_pct"] == row["pnl_pct"] else None}
             for row in ([] if val.empty else val.to_dict("records"))]
    entrada_c = {"data": hoje.isoformat(),
                 "carteira": {"valor_brl": round(res["valor_brl"], 1), "custo_brl": round(res["custo_brl"], 1), "pnl_brl": round(res["pnl_brl"], 1),
                              "por_tipo_brl": {k: round(v, 1) for k, v in res["por_tipo"].items()}, "sem_preco": res["sem_preco"]},
                 "posicoes": [{"ativo": r["ativo"], "tese_continua": r["tese_continua"] or "indisponivel"} for r in resultados],
                 "pesos": pesos,
                 "gatilhos_disparados": [{"ativo": d["ativo"], "descricao": d["descricao"]} for d in novos_disparos]}
    rc = ia.gerar_briefing_carteira(cliente, db, cfg, entrada_c, hoje)
    validos = sum(1 for r in resultados if r["valido"])
    return {"n": validos, "posicoes": resultados, "carteira_valida": bool(rc["valido"]), "custo_usd": ia.briefing_do_dia(db, hoje)["custo_usd"]}


def enviar_email(cfg: Config, db: Db, consulta: Consulta, carteira: Carteira, hoje: date, novos_disparos: list[dict]) -> dict:
    fx, _ = cambio_atual(consulta)
    res = resumo(valorizar(carteira.listar(), precos_atuais(consulta, carteira), fx, hoje))
    corpo = email_mod.corpo_briefing(ia.briefing_do_dia(db, hoje), novos_disparos, res)
    enviado = email_mod.enviar(cfg, f"mesa — briefing {hoje.isoformat()}", corpo)
    return {"n": 1 if enviado else 0, "enviado": enviado}


def manha(cfg: Config, agora: datetime | None = None, cliente_ia=None, so_briefing: bool = False) -> dict:
    db, consulta = Db(cfg.db_path), Consulta(cfg.dados_dir)
    carteira = Carteira(db)
    ctx = contexto(cfg, carteira, agora)
    hoje = ctx.agora.date()
    out = {}
    if not so_briefing:
        out["coletas"] = coletar(cfg, db, fontes_manha(cfg, consulta), ctx)
        out["calculo"] = calcular(cfg, db, consulta, carteira, ctx.agora)
    out["noticias"] = _etapa(db, "noticias", hoje, lambda: coletar_noticias(db, carteira))
    gat_res = _etapa(db, "gatilhos", hoje, lambda: avaliar_gatilhos(db, carteira, hoje))
    out["gatilhos"] = gat_res
    novos = gat_res.get("novos", [])
    do_dia = gat.do_dia(db, hoje)  # disparos do dia inteiro: reexecutar nao muda a entrada da IA
    out["briefing"] = _etapa(db, "briefing", hoje, lambda: gerar_briefings(cfg, db, consulta, carteira, hoje, do_dia, cliente_ia))
    out["email"] = _etapa(db, "email", hoje, lambda: enviar_email(cfg, db, consulta, carteira, hoje, novos))
    return out


def briefing(cfg: Config, agora: datetime | None = None, cliente_ia=None) -> dict:
    """So noticias + gatilhos + briefing + e-mail (sem recoletar fontes)."""
    return manha(cfg, agora, cliente_ia=cliente_ia, so_briefing=True)


def fechamento(cfg: Config, agora: datetime | None = None) -> dict:
    db, consulta = Db(cfg.db_path), Consulta(cfg.dados_dir)
    carteira = Carteira(db)
    ctx = contexto(cfg, carteira, agora)
    coletas = coletar(cfg, db, fontes_fechamento(), ctx)
    return {"coletas": coletas, "calculo": calcular(cfg, db, consulta, carteira, ctx.agora)}
