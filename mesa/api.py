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

from mesa import busca as bsc
from mesa import gatilhos as gat
from mesa import ia
from mesa import job
from mesa import noticias as noti
from mesa.armazenamento import Consulta, Db
from mesa.calendario import agora_brt
from mesa.carteira import Carteira, Posicao, resumo, somar_compras, valorizar
from mesa.config import Config
from mesa.fontes.yahoo import cotacao_atual, cotacao_benchmarks

log = logging.getLogger("mesa.api")


def _d(x) -> str:
    return pd.Timestamp(x).date().isoformat()


class CompraIn(BaseModel):
    data: date
    quantidade: float
    preco: float


class PosicaoIn(BaseModel):
    ativo: str = ""
    tipo: str
    identificador: str
    quantidade: float = 0            # quando vem `compras`, sai da soma delas
    preco_medio: float = 0
    data_compra: date | None = None
    tese: str | None = None
    busca: str = ""
    mandato: str = ""
    compras: list[CompraIn] = []


class TeseIn(BaseModel):
    texto: str


def criar_app(cfg: Config, db: Db | None = None, consulta: Consulta | None = None) -> FastAPI:
    app = FastAPI(title="mesa")
    db = db or Db(cfg.db_path)
    consulta = consulta or Consulta(cfg.dados_dir)
    carteira = Carteira(db)
    app.state.cfg, app.state.db, app.state.consulta, app.state.carteira = cfg, db, consulta, carteira
    try:
        consulta.duck()  # monta as views agora: senão a primeira busca do dia paga sozinha os ~120 ms
    except Exception as e:  # noqa: BLE001 - sem dados ainda nao e motivo para nao subir
        log.warning("views do DuckDB nao montaram no start: %s", e)
    app.state.cliente_ia = None  # injetavel nos testes
    sessoes: set[str] = set()
    trava_job = threading.Lock()
    cache_cotacao = {"em": None, "dados": {}}
    cache_bench = {"em": None, "dados": {}}

    @app.middleware("http")
    async def sem_cache(request: Request, chamar):
        """Sem isto o navegador reaproveita /carteira e /posicoes por heurística e mostra a carteira
        de antes da última alteração."""
        resposta = await chamar(request)
        resposta.headers["Cache-Control"] = "no-store"
        return resposta

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

    def _termos_do_mandato(pid: int, forcar: bool = False) -> None:
        """Se a posicao tem mandato e nao tem termos de busca (ou pediu para regerar), a IA extrai os termos."""
        p = carteira.obter(pid)
        if not p or not p.mandato or (p.busca and not forcar):
            return
        try:
            termos = ia.extrair_termos(app.state.cliente_ia or ia.ClienteOpenAI(cfg), p.mandato)
        except ia.IAIndisponivel as e:
            log.warning("termos do mandato: %s", e)
            return
        if termos:
            base = [p.identificador] if p.mercado in ("B3", "US") else []
            carteira.atualizar(pid, busca=";".join(base + termos))

    @app.get("/posicoes", dependencies=[Depends(autenticado)])
    def listar_posicoes():
        return [{**p.para_dict(), "tese": carteira.tese(p.id)} for p in carteira.listar()]

    @app.post("/posicoes", status_code=201, dependencies=[Depends(autenticado)])
    def criar_posicao(corpo: PosicaoIn):
        compras = [c.model_dump() for c in corpo.compras]
        if compras:
            try:
                corpo.quantidade, corpo.preco_medio, corpo.data_compra = somar_compras(compras)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
        if corpo.data_compra is None:
            raise HTTPException(status_code=422, detail="informe a data da compra")
        try:
            p = Posicao(corpo.ativo, corpo.tipo, corpo.identificador, corpo.quantidade, corpo.preco_medio, corpo.data_compra,
                        busca=corpo.busca, mandato=corpo.mandato)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        pid = carteira.criar(p, corpo.tese, compras or None)
        _termos_do_mandato(pid)
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
        if corpo.get("gerar_termos") or (corpo.get("mandato") and not corpo.get("busca")):
            _termos_do_mandato(pid, forcar=bool(corpo.get("gerar_termos")))
        p = carteira.obter(pid)
        return {**p.para_dict(), "tese": carteira.tese(pid)}

    @app.delete("/posicoes/{pid}", dependencies=[Depends(autenticado)])
    def excluir_posicao(pid: int):
        if carteira.obter(pid) is None:
            raise HTTPException(status_code=404, detail="posição não encontrada")
        carteira.excluir(pid)
        return {"ok": True}

    @app.get("/posicoes/{pid}/compras", dependencies=[Depends(autenticado)])
    def listar_compras(pid: int):
        if carteira.obter(pid) is None:
            raise HTTPException(status_code=404, detail="posição não encontrada")
        return carteira.compras(pid)

    @app.post("/posicoes/{pid}/compras", status_code=201, dependencies=[Depends(autenticado)])
    def nova_compra(pid: int, corpo: CompraIn):
        try:
            p = carteira.adicionar_compra(pid, corpo.data, corpo.quantidade, corpo.preco)
        except KeyError:
            raise HTTPException(status_code=404, detail="posição não encontrada")
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {**p.para_dict(), "compras": carteira.compras(pid)}

    @app.delete("/compras/{cid}", dependencies=[Depends(autenticado)])
    def excluir_compra(cid: int):
        try:
            p = carteira.remover_compra(cid)
        except KeyError:
            raise HTTPException(status_code=404, detail="compra não encontrada")
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {**p.para_dict(), "compras": carteira.compras(p.id)}

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
        # iterrows em 1.250 pregoes custava ~60 ms do /ativo; montar por coluna resolve.
        def _serie_json(df, colunas: dict[str, str]) -> list[dict]:
            if df.empty:
                return []
            saida = pd.DataFrame({"data": pd.to_datetime(df["data"]).dt.strftime("%Y-%m-%d")})
            for destino, origem in colunas.items():
                saida[destino] = pd.to_numeric(df[origem], errors="coerce")
            return saida.where(pd.notna(saida), None).to_dict("records")

        if p.tipo == "fundo":
            serie = _serie_json(consulta.cotas([p.identificador]), {"valor": "cota", "pl": "pl"})
        elif p.mercado in ("B3", "US"):
            serie = _serie_json(consulta.precos([p.identificador]),
                                {"abertura": "abertura", "maxima": "maxima", "minima": "minima",
                                 "fechamento": "fechamento", "ajustado": "ajustado", "volume": "volume"})
        else:
            serie = []
        return {**p.para_dict(), "tese": carteira.tese(pid), "historico_teses": carteira.historico_teses(pid),
                "compras": carteira.compras(pid),
                "metricas": db.metricas_recentes().get(pid), "serie": serie}

    @app.get("/buscar", dependencies=[Depends(autenticado)])
    def buscar(q: str, fontes: str = "todas"):
        return bsc.buscar(consulta, q, fontes)

    @app.get("/cotacao", dependencies=[Depends(autenticado)])
    def cotacao(tipo: str, identificador: str, data: date | None = None):
        if data is None:
            return bsc.cotacao(consulta, tipo, identificador)
        if data > agora_brt().date():
            raise HTTPException(status_code=422, detail="data de compra no futuro")
        return bsc.preco_em(consulta, tipo, identificador, data, dados_dir=cfg.dados_dir)

    @app.get("/macro", dependencies=[Depends(autenticado)])
    def macro(ao_vivo: bool = False):
        bench = job.benchmarks(consulta)
        out = {}
        for nome, s in bench.items():
            if nome == "CDI_diario" or s.empty:
                continue
            out[nome] = {"ultimo": float(s.iloc[-1]), "data": s.index[-1].date().isoformat(),
                         "var_1d": float((s.iloc[-1] / s.iloc[-2] - 1) * 100) if len(s) > 1 else None,
                         "var_1m": float((s.iloc[-1] / s[s.index <= s.index[-1] - pd.Timedelta(days=30)].iloc[-1] - 1) * 100)
                         if (s.index <= s.index[-1] - pd.Timedelta(days=30)).any() else None}
        if ao_vivo:
            agora = agora_brt()
            if cache_bench["em"] is None or (agora - cache_bench["em"]).total_seconds() > 60:
                cache_bench["dados"], cache_bench["em"] = cotacao_benchmarks(), agora
            for nome, q in cache_bench["dados"].items():
                if nome not in out or not q.get("preco"):
                    continue
                anterior = q.get("fechamento_anterior")
                out[nome] = {**out[nome], "ultimo": q["preco"], "ao_vivo": True, "hora": agora.strftime("%H:%M"),
                             "fechamento_anterior": anterior, "data_fechamento": out[nome]["data"],
                             "var_1d": (q["preco"] / anterior - 1) * 100 if anterior else out[nome]["var_1d"]}
        curvas = {}
        for pais in ("BR", "US"):
            # Só as duas datas usadas: a curva inteira são dezenas de milhares de linhas por país.
            datas = consulta._df("""SELECT max(data) AS ultima,
                                           max(CASE WHEN data <= (SELECT max(data) FROM curvas WHERE pais = ?) - INTERVAL 30 DAY
                                                    THEN data END) AS ha_um_mes
                                    FROM curvas WHERE pais = ?""", [pais, pais])
            if datas.empty or pd.isna(datas["ultima"].iloc[0]):
                continue
            ultima = pd.Timestamp(datas["ultima"].iloc[0])
            data_1m = None if pd.isna(datas["ha_um_mes"].iloc[0]) else pd.Timestamp(datas["ha_um_mes"].iloc[0])
            alvo = [d.date() for d in (ultima, data_1m) if d is not None]
            marcas = ", ".join("?" for _ in alvo)
            df = consulta._df(f"SELECT data, vencimento, titulo, taxa FROM curvas WHERE pais = ? AND data IN ({marcas})", [pais, *alvo])
            if df.empty:
                continue
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
