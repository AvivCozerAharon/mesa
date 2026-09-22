"""Briefing por IA: o modelo é o analista que prepara o briefing; a decisão é do usuário.

Contrato com o modelo (e é o contrato que o validador cobra):
- recebe SÓ fatos calculados (JSON arredondado), notícias numeradas, a tese e os gatilhos disparados;
- não recomenda comprar/vender/manter (regex derruba);
- toda afirmação sobre notícia cita [N#]; citação de id inexistente derruba;
- todo número no texto tem que existir na entrada (com tolerância de arredondamento) — número
  "inventado" derruba;
- sem notícia e sem gatilho, `tese_continua = nao_avaliavel`.
Saída inválida ganha UMA segunda chance com a lista de problemas; se falhar, fica `valido=0` e a UI
mostra só os fatos. Tokens, custo e latência ficam gravados por briefing. Mesma entrada no mesmo
dia (hash) não chama de novo.
"""
import hashlib
import json
import re
import time
from datetime import date

from mesa.armazenamento import Db, agora_utc
from mesa.config import Config

PROMPT_SISTEMA = """Você é o analista que prepara o briefing matinal de um investidor pessoa física sobre UMA posição da carteira dele.
Regras invioláveis:
1. Use APENAS os fatos e as notícias fornecidos no JSON de entrada. Não use conhecimento externo, não estime, não complete.
2. NUNCA recomende comprar, vender, manter, aumentar ou reduzir a posição. Você descreve; ele decide.
3. Toda afirmação baseada em notícia cita o id entre colchetes, ex.: [N2]. Só cite ids que existem na entrada.
4. Todo número que você escrever precisa aparecer na entrada (pode arredondar para 1 casa decimal). Prefira repetir o número exato.
4b. Se a entrada trouxer "mandato" (o que o fundo/ativo compra), trate as notícias sobre essa classe de ativo/setor como
   relevantes para a posição — a tese e o mandato são o critério de relevância.
5. "leitura_fundamentos": se a entrada trouxer "fundamentos"/"trimestres", descreva em até 60 palavras o que os números mostram
   (lucratividade, alavancagem, crescimento, distribuição de dividendos) — sem julgar caro/barato e sem recomendar. Sem fundamentos: "".
6. "tese_continua" responde se a TESE DO INVESTIDOR (texto fornecido) continua de pé à luz dos fatos/notícias:
   "sim" (nada nos fatos/notícias contradiz a tese), "revisar" (algo fornecido contradiz ou enfraquece a tese — diga o quê, com citação),
   "nao_avaliavel" (não há notícia nem gatilho relevante para julgar). Sem tese fornecida: "nao_avaliavel".
7. Português do Brasil, direto, sem floreio, sem cumprimentos.
Responda SOMENTE com JSON válido neste formato:
{"situacao": "até 90 palavras: onde o preço/cota está e o que mudou, com números da entrada",
 "leitura_fundamentos": "até 60 palavras sobre o que os fundamentos mostram, ou vazio",
 "tese_continua": "sim|revisar|nao_avaliavel",
 "justificativa": "até 60 palavras, citando [N#] quando usar notícia",
 "citacoes": ["N1"],
 "pontos_de_atencao": ["até 3 itens curtos, cada um ancorado em fato ou notícia citada"]}"""

PROMPT_CARTEIRA = """Você prepara o resumo matinal da carteira inteira de um investidor pessoa física.
Entrada: resumo da carteira (valores, pesos, variação), o veredito de tese de cada posição (já avaliado) e os gatilhos disparados.
Regras: só use o que está na entrada; nunca recomende comprar/vender/manter; todo número precisa existir na entrada; português direto.
Responda SOMENTE com JSON: {"resumo": "até 120 palavras", "olhar_hoje": ["nomes dos ativos que merecem atenção hoje, em ordem"], "citacoes": []}"""

RECOMENDACAO = re.compile(r"\b(compre|venda|vend[ae]r|comprar|mantenha|manter a posi|aumente|reduza|zere|desfa[çc]a|realize lucro)\b", re.I)
NUMERO = re.compile(r"(?<![A-Za-z#])[-+]?\d+(?:[.,]\d+)?")
CHAVES_POSICAO = {"situacao": str, "tese_continua": str, "justificativa": str, "citacoes": list, "pontos_de_atencao": list}
CHAVES_OPCIONAIS = {"leitura_fundamentos": str}
ENUM_TESE = {"sim", "revisar", "nao_avaliavel"}


class IAIndisponivel(Exception):
    pass


def _r1(x):
    return None if x is None else round(float(x), 1)


def montar_entrada(posicao: dict, tese: str | None, metricas: dict | None, noticias: list[dict], disparos: list[dict], hoje: date) -> dict:
    m = metricas or {}
    fatos = {"ativo": posicao["ativo"], "tipo": posicao["tipo"], "data_referencia": hoje.isoformat(),
             "preco_ou_cota_ultimo": _r1(m.get("ultimo")), "data_ultimo": m.get("data_ultimo"),
             "preco_medio_de_compra": _r1(posicao.get("preco_medio")), "data_compra": str(posicao.get("data_compra")),
             "retorno_desde_compra_pct": _r1(m.get("vs_preco_medio_pct") if "vs_preco_medio_pct" in m else (m.get("retornos") or {}).get("desde_compra")),
             "retornos_pct": {k: _r1(v) for k, v in (m.get("retornos") or {}).items()},
             "benchmark": m.get("benchmark"), "benchmark_retornos_pct": {k: _r1(v) for k, v in (m.get("bench_retornos") or {}).items()},
             "excesso_sobre_benchmark_pct": {k: _r1(v) for k, v in (m.get("excesso") or {}).items()},
             "drawdown_atual_pct": _r1((m.get("drawdown") or {}).get("atual")), "drawdown_maximo_pct": _r1((m.get("drawdown") or {}).get("maximo")),
             "vol_30d_pct": _r1(m.get("vol_30d")), "vol_12m_pct": _r1(m.get("vol_252d")),
             "media_50d": _r1(m.get("mm50")), "media_200d": _r1(m.get("mm200")),
             "distancia_do_topo_52s_pct": _r1((m.get("s52") or {}).get("pct_do_topo")), "distancia_do_fundo_52s_pct": _r1((m.get("s52") or {}).get("pct_do_fundo")),
             "beta_12m": _r1(m.get("beta_12m"))}
    if posicao["tipo"] == "fundo":
        fatos.update({"pct_do_benchmark": {k: _r1(v) for k, v in (m.get("pct_do_bench") or {}).items()},
                      "janelas_12m_batidas_pct": _r1((m.get("janelas_12m") or {}).get("pct")),
                      "pares_12m": m.get("pares", {}).get("12m"), "taxa_adm_pct": m.get("taxa_adm"), "taxa_perf_pct": m.get("taxa_perf"),
                      "pl_reais": _r1(m.get("pl")), "captacao_liquida_6m_reais": _r1(m.get("captacao_liquida_6m")),
                      "classe": m.get("classe"), "gestor": m.get("gestor")})
    if m.get("fundamentos"):
        f = m["fundamentos"]
        fatos["fundamentos"] = {k: _r1(f.get(k)) for k in ("pe", "pe_projetado", "pvp", "ev_ebitda", "margem_ebitda", "margem_liquida", "roe",
                                                           "divida_liq_ebitda", "dy", "payout", "cresc_receita", "cresc_lucro", "beta") if f.get(k) is not None}
        for k in ("receita", "ebitda", "fcf", "market_cap"):
            if f.get(k) is not None:
                fatos["fundamentos"][k + "_bi"] = _r1(f[k] / 1e9)
        fatos["fundamentos"]["moeda"] = f.get("moeda")
        fatos["fundamentos"]["ultimo_balanco"] = f.get("ultimo_balanco")
        fatos["fundamentos"]["unidades"] = "múltiplos em vezes; margens, roe, dy, payout e crescimento em %; valores _bi em bilhões"
    if m.get("trimestres"):
        fatos["trimestres_bi"] = {"moeda": (m.get("fundamentos") or {}).get("moeda_dre"),
                                  "valores": [{"trimestre": t["trimestre"], **{k: _r1(t[k] / 1e9) for k in ("receita", "ebitda", "lucro") if t.get(k) is not None}}
                                              for t in m["trimestres"][-6:]]}
    fatos = {k: v for k, v in fatos.items() if v not in (None, {}, [])}
    return {"fatos": fatos,
            "noticias": [{"id": f"N{i + 1}", "titulo": n["titulo"], "fonte": n.get("fonte") or "", "data": (n.get("publicada_em") or "")[:10]}
                         for i, n in enumerate(noticias)],
            "tese": {"texto": tese, "definida_em": posicao.get("tese_em")} if tese else None,
            "mandato": (posicao.get("mandato") or "").strip() or None,  # o que o fundo compra: define o que e noticia relevante
            "gatilhos_disparados": [{"regra": d["regra"], "descricao": d.get("descricao"), "detalhe": d.get("detalhe")} for d in disparos]}


PROMPT_TERMOS = """Você recebe a descrição do que um fundo ou ativo compra (mandato). Devolva de 3 a 6 termos de busca curtos
(1 a 3 palavras cada, em português, específicos: classes de ativo, instrumentos, setores, índices, gestora) que encontrem
notícias relevantes para quem tem esse produto. Nada genérico como "mercado" ou "economia".
Responda SOMENTE com JSON: {"termos": ["...", "..."]}"""


def extrair_termos(cliente, mandato: str) -> list[str]:
    """Termos de busca de notícias a partir do texto do mandato (uma chamada; sem chave, lista vazia)."""
    if not (mandato or "").strip():
        return []
    texto, *_ = cliente.completar(PROMPT_TERMOS, json.dumps({"mandato": mandato.strip()}, ensure_ascii=False))
    try:
        termos = json.loads(texto).get("termos", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    limpos = []
    for t in termos:
        t = str(t).strip().strip('"').strip()
        if 2 <= len(t) <= 40 and ";" not in t and t.lower() not in {x.lower() for x in limpos}:
            limpos.append(t)
    return limpos[:6]


def _numeros(obj) -> set[str]:
    """Todos os números (como strings normalizadas) presentes num objeto JSON."""
    out = set()

    def visita(x):
        if isinstance(x, dict):
            for k, v in x.items():
                visita(str(k))  # "vol_30d", "s52": os periodos das chaves tambem sao fatos da entrada
                visita(v)
        elif isinstance(x, list):
            for v in x:
                visita(v)
        elif isinstance(x, bool):
            return
        elif isinstance(x, (int, float)):
            out.update(_formas(x))
        elif isinstance(x, str):
            for m in NUMERO.finditer(x):
                try:
                    out.update(_formas(float(m.group().replace(",", "."))))
                except ValueError:
                    pass
    visita(obj)
    return out


def _formas(v: float) -> set[str]:
    a = abs(float(v))
    formas = {f"{a:.1f}", f"{a:.0f}", f"{round(a, 1):g}", f"{a:g}"}
    if a >= 1000:
        formas.add(f"{a / 1000:.1f}")   # 22308 -> "22.3" (mil)
        formas.add(f"{a / 1_000_000:.1f}")
    return formas


def validar(entrada: dict, saida, chaves: dict = CHAVES_POSICAO) -> tuple[bool, list[str]]:
    problemas = []
    if not isinstance(saida, dict):
        return False, ["saída não é um objeto JSON"]
    for k, t in chaves.items():
        if k not in saida or not isinstance(saida[k], t):
            problemas.append(f"campo '{k}' ausente ou de tipo errado")
    for k, t in CHAVES_OPCIONAIS.items():
        if k in saida and not isinstance(saida[k], t):
            problemas.append(f"campo '{k}' de tipo errado")
    if problemas:
        return False, problemas
    ids = {n["id"] for n in entrada.get("noticias", [])}
    citadas = set(saida.get("citacoes", []))
    if not citadas <= ids:
        problemas.append(f"citações inexistentes: {sorted(citadas - ids)}")
    texto = " ".join([saida.get("situacao", ""), saida.get("justificativa", ""), saida.get("resumo", ""), saida.get("leitura_fundamentos", "") or ""]
                     + [str(p) for p in saida.get("pontos_de_atencao", [])])
    no_texto = set(re.findall(r"\[(N\d+)\]", texto))
    if not no_texto <= ids:
        problemas.append(f"texto cita id inexistente: {sorted(no_texto - ids)}")
    if not no_texto <= citadas:
        problemas.append(f"texto cita {sorted(no_texto - citadas)} sem listar em 'citacoes'")
    if "tese_continua" in chaves and saida["tese_continua"] not in ENUM_TESE:
        problemas.append("tese_continua fora de {sim, revisar, nao_avaliavel}")
    if "tese_continua" in chaves and not entrada.get("noticias") and not entrada.get("gatilhos_disparados") and saida["tese_continua"] != "nao_avaliavel":
        problemas.append("sem notícia e sem gatilho, tese_continua deve ser nao_avaliavel")
    if RECOMENDACAO.search(texto):
        problemas.append("texto contém recomendação de compra/venda/manutenção")
    permitidos = _numeros(entrada)
    for m in NUMERO.finditer(texto):
        bruto = m.group()
        try:
            v = float(bruto.replace(",", "."))
        except ValueError:
            continue
        decimal = "." in bruto or "," in bruto
        if abs(v) < 10 and not decimal:
            continue  # "3 itens", "2 semanas": inteiros pequenos nao sao "fatos"
        # com casa decimal, a forma de 1 casa tem que existir; inteiro aceita o arredondamento da entrada
        formas = {f"{abs(v):.1f}", f"{abs(v):g}"} if decimal else _formas(v)
        if not (formas & permitidos):
            problemas.append(f"número não presente na entrada: {bruto}")
    return not problemas, problemas


class ClienteOpenAI:
    def __init__(self, cfg: Config):
        if not cfg.openai_api_key:
            raise IAIndisponivel("OPENAI_API_KEY não configurada")
        from openai import OpenAI
        self._cli = OpenAI(api_key=cfg.openai_api_key)
        self.modelo = cfg.openai_modelo

    def completar(self, sistema: str, usuario: str) -> tuple[str, int, int, int]:
        inicio = time.time()
        try:
            r = self._cli.chat.completions.create(model=self.modelo, response_format={"type": "json_object"},
                                                  messages=[{"role": "system", "content": sistema}, {"role": "user", "content": usuario}])
        except Exception as e:  # noqa: BLE001 - qualquer falha da API vira IAIndisponivel
            raise IAIndisponivel(f"{type(e).__name__}: {e}"[:300]) from e
        u = r.usage
        return r.choices[0].message.content or "", int(getattr(u, "prompt_tokens", 0) or 0), int(getattr(u, "completion_tokens", 0) or 0), int((time.time() - inicio) * 1000)


def _hash(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _existente(db: Db, data: date, posicao_id: int | None, h: str, modelo: str) -> dict | None:
    r = db.con.execute("SELECT * FROM briefings WHERE data = ? AND posicao_id IS ? AND valido = 1 AND modelo = ? ORDER BY id DESC LIMIT 1",
                       (data.isoformat(), posicao_id, modelo)).fetchone()
    if r and json.loads(r["entrada"]).get("_hash") == h:
        d = dict(r)
        d["saida"] = json.loads(d["saida"]) if d["saida"] else None
        d["reaproveitado"] = True
        return d
    return None


def _gerar(cliente, db: Db, cfg: Config, sistema: str, entrada: dict, chaves: dict, data: date, posicao_id: int | None) -> dict:
    h = _hash(entrada)
    pronto = _existente(db, data, posicao_id, h, getattr(cliente, "modelo", "?"))  # cache por modelo: briefing de outro modelo nao vale
    if pronto:
        return pronto
    entrada_gravada = {**entrada, "_hash": h}
    registro = {"data": data.isoformat(), "posicao_id": posicao_id, "entrada": json.dumps(entrada_gravada, ensure_ascii=False, default=str),
                "saida": None, "modelo": getattr(cliente, "modelo", "?"), "tokens_in": 0, "tokens_out": 0, "custo_usd": 0.0,
                "latencia_ms": 0, "valido": 0, "erro": None}
    usuario = json.dumps(entrada, ensure_ascii=False, default=str)
    problemas: list[str] = []
    saida = None
    try:
        for tentativa in range(2):
            msg = usuario if tentativa == 0 else (usuario + "\n\nSua resposta anterior foi rejeitada pelo validador por: "
                                                  + "; ".join(problemas) + ". Corrija e responda de novo só com o JSON.")
            texto, ti, to, lat = cliente.completar(sistema, msg)
            registro["tokens_in"] += ti
            registro["tokens_out"] += to
            registro["latencia_ms"] += lat
            try:
                saida = json.loads(texto)
            except json.JSONDecodeError:
                saida, problemas = None, ["resposta não é JSON"]
                continue
            ok, problemas = validar(entrada, saida, chaves)
            if ok:
                registro["valido"] = 1
                break
        registro["saida"] = json.dumps(saida, ensure_ascii=False) if saida is not None else None
        if not registro["valido"]:
            registro["erro"] = "inválido após 2 tentativas: " + "; ".join(problemas)
    except IAIndisponivel as e:
        registro["erro"] = str(e)
    registro["custo_usd"] = registro["tokens_in"] * cfg.openai_preco_in / 1e6 + registro["tokens_out"] * cfg.openai_preco_out / 1e6
    cur = db.con.execute("INSERT INTO briefings (data, posicao_id, entrada, saida, modelo, tokens_in, tokens_out, custo_usd, latencia_ms, valido, erro, criado_em)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (registro["data"], registro["posicao_id"], registro["entrada"], registro["saida"], registro["modelo"], registro["tokens_in"],
                          registro["tokens_out"], registro["custo_usd"], registro["latencia_ms"], registro["valido"], registro["erro"], agora_utc().isoformat()))
    db.con.commit()
    registro["id"] = cur.lastrowid
    registro["saida"] = saida if registro["valido"] else None
    registro["reaproveitado"] = False
    return registro


def gerar_briefing_posicao(cliente, db: Db, cfg: Config, posicao: dict, entrada: dict, hoje: date) -> dict:
    return _gerar(cliente, db, cfg, PROMPT_SISTEMA, entrada, CHAVES_POSICAO, hoje, posicao["id"])


CHAVES_CARTEIRA = {"resumo": str, "olhar_hoje": list, "citacoes": list}


def gerar_briefing_carteira(cliente, db: Db, cfg: Config, entrada: dict, hoje: date) -> dict:
    return _gerar(cliente, db, cfg, PROMPT_CARTEIRA, entrada, CHAVES_CARTEIRA, hoje, None)


def briefing_do_dia(db: Db, data: date | None = None) -> dict:
    if data is None:
        r = db.con.execute("SELECT max(data) FROM briefings").fetchone()
        if not r or not r[0]:
            return {"data": None, "carteira": None, "posicoes": []}
        data = date.fromisoformat(r[0])
    rows = db.con.execute("""SELECT b.*, p.ativo FROM briefings b LEFT JOIN posicoes p ON p.id = b.posicao_id
                             WHERE b.data = ? AND b.id IN (SELECT max(id) FROM briefings WHERE data = ? GROUP BY posicao_id)
                             ORDER BY b.posicao_id""", (data.isoformat(), data.isoformat())).fetchall()
    carteira, posicoes = None, []
    for r in rows:
        d = dict(r)
        d["saida"] = json.loads(d["saida"]) if d["saida"] and d["valido"] else None
        entrada = json.loads(d.pop("entrada"))
        entrada.pop("_hash", None)
        d["entrada"] = entrada
        if d["posicao_id"] is None:
            carteira = d
        else:
            posicoes.append(d)
    custo = db.con.execute("SELECT sum(custo_usd), sum(tokens_in), sum(tokens_out) FROM briefings WHERE data = ?", (data.isoformat(),)).fetchone()
    return {"data": data.isoformat(), "carteira": carteira, "posicoes": posicoes,
            "custo_usd": custo[0] or 0.0, "tokens_in": custo[1] or 0, "tokens_out": custo[2] or 0}
