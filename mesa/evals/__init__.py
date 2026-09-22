"""Evals: medem o que o sistema promete.

- casamento notícia↔ativo (precisão/recall) sobre 30 títulos rotulados — sem rede;
- validador da saída da IA sobre saídas boas/ruins construídas — sem rede;
- concordância da IA com o rótulo humano em "a tese continua de pé?" (só com chave configurada),
  mais a taxa de saída válida na 1ª tentativa.
"""
import json
from datetime import date
from pathlib import Path

from mesa import ia
from mesa.armazenamento import Db, agora_utc
from mesa.carteira import Posicao
from mesa.noticias import casar

AQUI = Path(__file__).parent
POSICOES_EVAL = [
    Posicao("", "acao", "PETR4", 1, 1, date(2025, 1, 1), busca="PETR4;Petrobras"),
    Posicao("", "acao", "VALE3", 1, 1, date(2025, 1, 1), busca="VALE3;Vale S.A."),
    Posicao("", "fii", "HGLG11", 1, 1, date(2025, 1, 1), busca="HGLG11"),
    Posicao("", "acao_us", "AAPL", 1, 1, date(2025, 1, 1), busca="AAPL;Apple"),
    Posicao("", "etf_us", "VOO", 1, 1, date(2025, 1, 1), busca="VOO;S&P 500"),
    Posicao("CSHG Verde 30 FIC FIM", "fundo", "22.215.116/0001-80", 1, 1, date(2025, 1, 1), busca="Verde Asset;Stuhlberger;Fundo Verde"),
    Posicao("", "tesouro", "Tesouro IPCA+ 2035", 1, 1, date(2025, 1, 1), busca="Tesouro IPCA+;NTN-B;Tesouro"),
]


def rodar_casamento() -> dict:
    casos = json.loads((AQUI / "casos_casamento.json").read_text(encoding="utf-8"))
    vp = fp = fn = 0
    erros = []
    for c in casos:
        obtidos = {ident for ident, _, _ in casar(c["titulo"], POSICOES_EVAL)}
        esperados = set(c["esperados"])
        vp += len(obtidos & esperados)
        fp += len(obtidos - esperados)
        fn += len(esperados - obtidos)
        if obtidos != esperados:
            erros.append({"titulo": c["titulo"], "esperados": sorted(esperados), "obtidos": sorted(obtidos)})
    precisao = vp / (vp + fp) if vp + fp else 1.0
    recall = vp / (vp + fn) if vp + fn else 1.0
    return {"n": len(casos), "precisao": round(precisao, 3), "recall": round(recall, 3), "erros": erros}


def _entrada_base(sem_noticias: bool = False) -> dict:
    pos = {"id": 1, "ativo": "PETR4", "tipo": "acao", "preco_medio": 32.1, "data_compra": "2025-02-14"}
    met = {"ultimo": 48.0, "data_ultimo": "2026-09-21", "vs_preco_medio_pct": 49.53, "retornos": {"1m": 3.7, "12m": 69.5},
           "benchmark": "IBOV", "bench_retornos": {"1m": 1.2, "12m": 20.0}, "excesso": {"1m": 2.5, "12m": 49.5},
           "drawdown": {"atual": -4.8, "maximo": -30.2}, "vol_30d": 22.1, "vol_252d": 28.4, "mm50": 46.0, "mm200": 41.0,
           "s52": {"pct_do_topo": -5.0, "pct_do_fundo": 40.0}, "beta_12m": 1.1}
    noticias = [] if sem_noticias else [{"id": 10, "titulo": "Petrobras anuncia dividendos de R$ 1,50 por ação", "fonte": "Valor", "publicada_em": "2026-09-21T10:00:00"}]
    return ia.montar_entrada(pos, "Petróleo alto e dividendos", met, noticias, [], date(2026, 9, 22))


def rodar_validacao() -> dict:
    casos = json.loads((AQUI / "casos_validacao.json").read_text(encoding="utf-8"))
    acertos, erros = 0, []
    for c in casos:
        ok, problemas = ia.validar(_entrada_base(c.get("sem_noticias", False)), c["saida"])
        if ok == c["valida"]:
            acertos += 1
        else:
            erros.append({"nome": c["nome"], "esperado": c["valida"], "obtido": ok, "problemas": problemas})
    return {"n": len(casos), "acertos": acertos, "erros": erros}


def rodar_tese(cliente, db: Db, cfg) -> dict:
    casos = json.loads((AQUI / "casos_tese.json").read_text(encoding="utf-8"))
    met = {"ultimo": 48.0, "data_ultimo": "2026-09-21", "vs_preco_medio_pct": 10.0, "retornos": {"1m": 1.0, "12m": 15.0},
           "drawdown": {"atual": -3.0, "maximo": -20.0}, "vol_30d": 20.0, "vol_252d": 25.0, "s52": {"pct_do_topo": -8.0, "pct_do_fundo": 30.0}}
    concordou = validos_1a = 0
    detalhes = []
    for i, c in enumerate(casos):
        pos = {"id": 1000 + i, "ativo": "ATIVO", "tipo": "acao", "preco_medio": 43.6, "data_compra": "2025-01-01"}
        noticias = [{"id": k + 1, "titulo": t, "fonte": "eval", "publicada_em": "2026-09-21T10:00:00"} for k, t in enumerate(c["noticias"])]
        entrada = ia.montar_entrada(pos, c["tese"], met, noticias, c["gatilhos"], date(2026, 9, 22))
        r = ia.gerar_briefing_posicao(cliente, db, cfg, pos, entrada, date(2026, 9, 22))
        obtido = (r.get("saida") or {}).get("tese_continua")
        ok = obtido == c["esperado"]
        concordou += ok
        validos_1a += bool(r.get("valido")) and r.get("tokens_in", 0) > 0 and "2 tentativas" not in (r.get("erro") or "") and not r.get("_retentou", False)
        detalhes.append({"nome": c["nome"], "esperado": c["esperado"], "obtido": obtido, "valido": bool(r.get("valido")), "erro": r.get("erro")})
    return {"n": len(casos), "concordancia": round(concordou / len(casos), 3) if casos else None,
            "validos": sum(1 for d in detalhes if d["valido"]), "detalhes": detalhes}


def registrar(db: Db, suite: str, resultado: dict) -> None:
    db.con.execute("INSERT INTO evals (data, suite, casos, acertos, detalhe) VALUES (?,?,?,?,?)",
                   (agora_utc().date().isoformat(), suite, resultado.get("n"),
                    resultado.get("acertos", resultado.get("validos")), json.dumps(resultado, ensure_ascii=False)))
    db.con.commit()
