"""API HTTP + scheduler. Senha única (MESA_SENHA) em cookie; sem senha configurada, tudo aberto (dev)."""
import logging
import secrets
import tempfile
import threading
from datetime import date, datetime

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from mesa import gatilhos as gat
from mesa import ia
from mesa import job
from mesa import noticias as noti
from mesa.armazenamento import Consulta, Db
from mesa.calendario import agora_brt
from mesa.carteira import Carteira, Posicao, resumo, valorizar
from mesa.config import Config
from mesa.fontes.yahoo import cotacao_atual

log = logging.getLogger("mesa.api")


def _d(x) -> str:
    return pd.Timestamp(x).date().isoformat()


class PosicaoIn(BaseModel):
    ativo: str = ""
    tipo: str
    identificador: str
    quantidade: float
    preco_medio: float
    data_compra: date
    tese: str | None = None
    busca: str = ""


class TeseIn(BaseModel):
    texto: str


def criar_app(cfg: Config, db: Db | None = None, consulta: Consulta | None = None) -> FastAPI:
    app = FastAPI(title="mesa")
    db = db or Db(cfg.db_path)
    consulta = consulta or Consulta(cfg.dados_dir)
    carteira = Carteira(db)
    app.state.cfg, app.state.db, app.state.consulta, app.state.carteira = cfg, db, consulta, carteira
    sessoes: set[str] = set()
    trava_job = threading.Lock()
    cache_cotacao = {"em": None, "dados": {}}

    def autenticado(request: Request):
        if not cfg.senha:
            return True
        tok = request.cookies.get("mesa_sessao")
        if tok in sessoes:
            return True
        raise HTTPException(status_code=401, detail="faça login")

    @app.post("/login")
    def login(corpo: dict, response: Response):
        if not cfg.senha or not secrets.compare_digest(corpo.get("senha", ""), cfg.senha):
            raise HTTPException(status_code=401, detail="senha incorreta")
        tok = secrets.token_urlsafe(32)
        sessoes.add(tok)
        response.set_cookie("mesa_sessao", tok, httponly=True, samesite="strict", max_age=30 * 86400)
        return {"ok": True}

    @app.get("/saude")
    def saude():
        return {"ok": True, "agora": agora_brt().isoformat(), "posicoes": len(carteira.listar())}

    @app.get("/carteira", dependencies=[Depends(autenticado)])
    def get_carteira(ao_vivo: bool = False):
        pos = carteira.listar()
        precos = job.precos_atuais(consulta, carteira)
        fx, data_fx = job.cambio_atual(consulta)
        hoje = agora_brt().date()
        if ao_vivo:
            agora = agora_brt()
            if cache_cotacao["em"] is None or (agora - cache_cotacao["em"]).total_seconds() > 60:
                cache_cotacao["dados"] = cotacao_atual(carteira.tickers() + [("BRL=X", "FX")])
                cache_cotacao["em"] = agora
            for ident, q in cache_cotacao["dados"].items():
                if ident == "BRL=X":
                    fx, data_fx = q["preco"], hoje
                else:
                    precos[ident] = (q["preco"], hoje)
        val = valorizar(pos, precos, fx, hoje)
        metricas = db.metricas_recentes()
        linhas = []
        for r in ([] if val.empty else val.to_dict("records")):
            r = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in r.items()}
            r["metricas"] = metricas.get(r["id"])
            r["tese"] = carteira.tese(r["id"])
            linhas.append(r)
        return {"hoje": hoje.isoformat(), "cambio": fx, "data_cambio": data_fx, "resumo": resumo(val), "posicoes": linhas}

    @app.get("/posicoes", dependencies=[Depends(autenticado)])
    def listar_posicoes():
        return [{**p.para_dict(), "tese": carteira.tese(p.id)} for p in carteira.listar()]

    @app.post("/posicoes", status_code=201, dependencies=[Depends(autenticado)])
    def criar_posicao(corpo: PosicaoIn):
        try:
            p = Posicao(corpo.ativo, corpo.tipo, corpo.identificador, corpo.quantidade, corpo.preco_medio, corpo.data_compra, busca=corpo.busca)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        pid = carteira.criar(p, corpo.tese)
        return {**carteira.obter(pid).para_dict(), "tese": carteira.tese(pid)}

    @app.put("/posicoes/{pid}", dependencies=[Depends(autenticado)])
    def atualizar_posicao(pid: int, corpo: dict):
        try:
            p = carteira.atualizar(pid, **{k: v for k, v in corpo.items() if k != "tese"})
        except KeyError:
            raise HTTPException(status_code=404, detail="posição não encontrada")
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if corpo.get("tese") is not None and corpo["tese"].strip() != (carteira.tese(pid) or ""):
            carteira.definir_tese(pid, corpo["tese"])
        return {**p.para_dict(), "tese": carteira.tese(pid)}

    @app.delete("/posicoes/{pid}", dependencies=[Depends(autenticado)])
    def excluir_posicao(pid: int):
        if carteira.obter(pid) is None:
            raise HTTPException(status_code=404, detail="posição não encontrada")
        carteira.excluir(pid)
        return {"ok": True}

    @app.get("/posicoes/{pid}/tese", dependencies=[Depends(autenticado)])
    def historico_tese(pid: int):
        return carteira.historico_teses(pid)

    @app.post("/posicoes/{pid}/tese", dependencies=[Depends(autenticado)])
    def nova_tese(pid: int, corpo: TeseIn):
        if carteira.obter(pid) is None:
            raise HTTPException(status_code=404, detail="posição não encontrada")
        carteira.definir_tese(pid, corpo.texto)
        return {"tese": carteira.tese(pid)}

    @app.post("/posicoes/importar", dependencies=[Depends(autenticado)])
    async def importar(arquivo: UploadFile):
        conteudo = await arquivo.read()
        with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as fh:
            fh.write(conteudo)
            caminho = fh.name
        try:
            n = carteira.importar_csv(caminho)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {"importadas": n}

    @app.get("/ativo/{pid}", dependencies=[Depends(autenticado)])
    def ativo(pid: int):
        p = carteira.obter(pid)
        if p is None:
            raise HTTPException(status_code=404, detail="posição não encontrada")
        if p.tipo == "fundo":
            df = consulta.cotas([p.identificador])
            serie = [] if df.empty else [{"data": _d(r["data"]), "valor": float(r["cota"]), "pl": float(r["pl"])} for _, r in df.iterrows()]
        elif p.mercado in ("B3", "US"):
            df = consulta.precos([p.identificador])
            serie = [] if df.empty else [{"data": _d(r["data"]), "abertura": r["abertura"], "maxima": r["maxima"], "minima": r["minima"],
                                          "fechamento": r["fechamento"], "ajustado": r["ajustado"], "volume": r["volume"]} for _, r in df.iterrows()]
        else:
            serie = []
        return {**p.para_dict(), "tese": carteira.tese(pid), "historico_teses": carteira.historico_teses(pid),
                "metricas": db.metricas_recentes().get(pid), "serie": serie}

    @app.get("/macro", dependencies=[Depends(autenticado)])
    def macro():
        bench = job.benchmarks(consulta)
        out = {}
        for nome, s in bench.items():
            if nome == "CDI_diario" or s.empty:
                continue
            out[nome] = {"ultimo": float(s.iloc[-1]), "data": s.index[-1].date().isoformat(),
                         "var_1d": float((s.iloc[-1] / s.iloc[-2] - 1) * 100) if len(s) > 1 else None,
                         "var_1m": float((s.iloc[-1] / s[s.index <= s.index[-1] - pd.Timedelta(days=30)].iloc[-1] - 1) * 100)
                         if (s.index <= s.index[-1] - pd.Timedelta(days=30)).any() else None}
        curvas = {}
        for pais in ("BR", "US"):
            df = consulta._df("SELECT data, vencimento, titulo, taxa FROM curvas WHERE pais = ? ORDER BY data", [pais])
            if df.empty:
                continue
            ultima = pd.Timestamp(df["data"].max())
            um_mes = df[pd.to_datetime(df["data"]) <= ultima - pd.Timedelta(days=30)]
            data_1m = pd.Timestamp(um_mes["data"].max()) if not um_mes.empty else None
            hoje_c = df[pd.to_datetime(df["data"]) == ultima]
            antes = df[pd.to_datetime(df["data"]) == data_1m] if data_1m is not None else pd.DataFrame()
            curvas[pais] = {"data": ultima.date().isoformat(), "data_1m": data_1m.date().isoformat() if data_1m is not None else None,
                            "pontos": [{"vencimento": str(r["vencimento"]), "titulo": r["titulo"], "taxa": r["taxa"]} for _, r in hoje_c.iterrows()],
                            "pontos_1m": [{"vencimento": str(r["vencimento"]), "titulo": r["titulo"], "taxa": r["taxa"]} for _, r in antes.iterrows()]}
        return {"benchmarks": out, "curvas": curvas}

    @app.get("/operacao", dependencies=[Depends(autenticado)])
    def operacao():
        return {"coletas": db.ultimas_coletas(60), "agora": agora_brt().isoformat()}

    @app.get("/briefing", dependencies=[Depends(autenticado)])
    def briefing(data: str | None = None):
        return ia.briefing_do_dia(db, date.fromisoformat(data) if data else None)

    @app.get("/noticias", dependencies=[Depends(autenticado)])
    def noticias(posicao: int | None = None, dias: int = 7):
        if posicao is None:
            return noti.gerais(db, dias=dias, limite=40)
        p = carteira.obter(posicao)
        if p is None:
            raise HTTPException(status_code=404, detail="posição não encontrada")
        return noti.recentes(db, p.identificador, dias=dias, limite=40)

    @app.get("/gatilhos/regras")
    def regras():
        return {"regras": list(gat.REGRAS), "descricoes": gat.DESCRICOES, "padrao": gat.PADRAO}

    @app.get("/gatilhos", dependencies=[Depends(autenticado)])
    def listar_gatilhos(posicao: int | None = None):
        return gat.listar(db, posicao)

    @app.post("/gatilhos", status_code=201, dependencies=[Depends(autenticado)])
    def criar_gatilho(corpo: dict):
        try:
            gid = gat.criar(db, corpo.get("posicao_id"), corpo["regra"], corpo.get("parametros") or {})
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {"id": gid}

    @app.delete("/gatilhos/{gid}", dependencies=[Depends(autenticado)])
    def excluir_gatilho(gid: int):
        gat.excluir(db, gid)
        return {"ok": True}

    @app.get("/disparos", dependencies=[Depends(autenticado)])
    def disparos(vistos: bool = False):
        return gat.pendentes(db, incluir_vistos=vistos)

    @app.post("/disparos/{did}/visto", dependencies=[Depends(autenticado)])
    def visto(did: int):
        gat.marcar_visto(db, did)
        return {"ok": True}

    @app.post("/job/{nome}", dependencies=[Depends(autenticado)])
    def rodar_job(nome: str):
        if nome not in ("manha", "fechamento", "briefing"):
            raise HTTPException(status_code=404, detail="job desconhecido")
        if not trava_job.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="já há um job rodando")
        try:
            return getattr(job, nome)(cfg)
        finally:
            trava_job.release()

    @app.get("/", response_class=HTMLResponse)
    def index():
        from pathlib import Path
        pagina = Path(__file__).parent / "static" / "index.html"
        if pagina.exists():
            return pagina.read_text(encoding="utf-8")
        return "<h1>mesa</h1><p>UI em construção — veja <a href='/carteira'>/carteira</a>.</p>"

    return app


def iniciar_scheduler(cfg: Config):
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    sch = BackgroundScheduler(timezone=cfg.fuso)
    h, m = cfg.hora_manha.split(":")
    sch.add_job(lambda: job.manha(cfg), CronTrigger(hour=int(h), minute=int(m), day_of_week="mon-fri", timezone=cfg.fuso), id="manha")
    h, m = cfg.hora_fechamento_b3.split(":")
    sch.add_job(lambda: job.fechamento(cfg), CronTrigger(hour=int(h), minute=int(m), day_of_week="mon-fri", timezone=cfg.fuso), id="fechamento_b3")
    h, m = cfg.hora_fechamento_us.split(":")
    sch.add_job(lambda: job.fechamento(cfg), CronTrigger(hour=int(h), minute=int(m), day_of_week="mon-fri", timezone="America/New_York"), id="fechamento_us")
    sch.start()
    log.info("scheduler: manha %s BRT, fechamento B3 %s BRT, fechamento US %s ET", cfg.hora_manha, cfg.hora_fechamento_b3, cfg.hora_fechamento_us)
    return sch
