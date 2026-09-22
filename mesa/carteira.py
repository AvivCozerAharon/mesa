"""Carteira: posições com CRUD (SQLite), tese com histórico, importação CSV e valorização em BRL.

Tipos: acao, fii, etf, bdr (B3, BRL) · acao_us, etf_us (US, USD) · fundo (CVM, BRL, cota) ·
tesouro (TD, BRL, PU). A tese nunca é sobrescrita: cada edição vira uma linha nova em `teses`, para
a IA (e o autor) verem o que mudou no raciocínio ao longo do tempo.
"""
import csv
import re
from dataclasses import asdict, dataclass
from datetime import date

import pandas as pd

from mesa.armazenamento import Db, agora_utc

TIPOS = {"acao": ("B3", "BRL"), "fii": ("B3", "BRL"), "etf": ("B3", "BRL"), "bdr": ("B3", "BRL"),
         "acao_us": ("US", "USD"), "etf_us": ("US", "USD"), "fundo": ("CVM", "BRL"), "tesouro": ("TD", "BRL")}
RE_TICKER_B3 = re.compile(r"^[A-Z]{4}\d{1,2}$")
RE_TICKER_US = re.compile(r"^[A-Z.\-]{1,6}$")
RE_TESOURO = re.compile(r"^Tesouro .+ \d{4}$")


def cnpj_valido(doc: str) -> bool:
    d = re.sub(r"\D", "", doc or "")
    if len(d) != 14 or d == d[0] * 14:
        return False
    def dv(nums, pesos):
        s = sum(int(n) * p for n, p in zip(nums, pesos))
        r = s % 11
        return "0" if r < 2 else str(11 - r)
    p1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    p2 = [6] + p1
    return d[12] == dv(d[:12], p1) and d[13] == dv(d[:13], p2)


@dataclass
class Posicao:
    ativo: str                  # rótulo: ticker, nome do fundo, nome do título
    tipo: str
    identificador: str          # ticker | CNPJ (dígitos) | nome do título do Tesouro
    quantidade: float
    preco_medio: float
    data_compra: date
    id: int | None = None
    mercado: str = ""
    moeda: str = ""
    ativa: bool = True
    busca: str = ""             # termos de busca de noticias separados por ";" (ex.: "PETR4;Petrobras")
    mandato: str = ""           # o que o fundo/ativo compra, em texto livre; a IA extrai termos de busca daqui

    def __post_init__(self):
        if self.tipo not in TIPOS:
            raise ValueError(f"tipo inválido: {self.tipo!r} (use {', '.join(TIPOS)})")
        self.mercado, self.moeda = TIPOS[self.tipo]
        self.ativo = self.ativo.strip()
        self.identificador = self.identificador.strip()
        if self.tipo in ("acao", "fii", "etf", "bdr"):
            self.identificador = self.identificador.upper()
            if not RE_TICKER_B3.match(self.identificador):
                raise ValueError(f"ticker B3 inválido: {self.identificador!r}")
            self.ativo = self.ativo.upper() or self.identificador
        elif self.tipo in ("acao_us", "etf_us"):
            self.identificador = self.identificador.upper()
            if not RE_TICKER_US.match(self.identificador):
                raise ValueError(f"ticker US inválido: {self.identificador!r}")
            self.ativo = self.ativo.upper() or self.identificador
        elif self.tipo == "fundo":
            self.identificador = re.sub(r"\D", "", self.identificador)
            if not cnpj_valido(self.identificador):
                raise ValueError(f"CNPJ inválido: {self.identificador!r}")
        elif self.tipo == "tesouro":
            if not RE_TESOURO.match(self.identificador):
                raise ValueError(f"título do Tesouro inválido: {self.identificador!r} (ex.: 'Tesouro IPCA+ 2035')")
            self.ativo = self.ativo or self.identificador
        if self.quantidade <= 0 or self.preco_medio <= 0:
            raise ValueError("quantidade e preço médio precisam ser positivos")
        if isinstance(self.data_compra, str):
            self.data_compra = date.fromisoformat(self.data_compra)

    def para_dict(self) -> dict:
        d = asdict(self)
        d["data_compra"] = self.data_compra.isoformat()
        return d


class Carteira:
    def __init__(self, db: Db):
        self._db = db

    def criar(self, p: Posicao, tese: str | None = None) -> int:
        ts = agora_utc().isoformat()
        cur = self._db.con.execute(
            "INSERT INTO posicoes (ativo, tipo, mercado, identificador, quantidade, preco_medio, moeda, data_compra, ativa, criada_em, atualizada_em, busca, mandato)"
            " VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)",
            (p.ativo, p.tipo, p.mercado, p.identificador, p.quantidade, p.preco_medio, p.moeda, p.data_compra.isoformat(), ts, ts, p.busca or "", p.mandato or ""))
        self._db.con.commit()
        pid = cur.lastrowid
        if tese:
            self.definir_tese(pid, tese)
        return pid

    def obter(self, pid: int) -> Posicao | None:
        r = self._db.con.execute("SELECT * FROM posicoes WHERE id = ?", (pid,)).fetchone()
        return self._de_linha(r) if r else None

    @staticmethod
    def _de_linha(r) -> Posicao:
        return Posicao(id=r["id"], ativo=r["ativo"], tipo=r["tipo"], identificador=r["identificador"],
                       quantidade=r["quantidade"], preco_medio=r["preco_medio"], data_compra=date.fromisoformat(r["data_compra"]),
                       ativa=bool(r["ativa"]), busca=r["busca"] if "busca" in r.keys() else "",
                       mandato=r["mandato"] if "mandato" in r.keys() else "")

    def listar(self, ativas: bool = True) -> list[Posicao]:
        sql = "SELECT * FROM posicoes" + (" WHERE ativa = 1" if ativas else "") + " ORDER BY tipo, ativo"
        return [self._de_linha(r) for r in self._db.con.execute(sql).fetchall()]

    def atualizar(self, pid: int, **campos) -> Posicao:
        atual = self.obter(pid)
        if atual is None:
            raise KeyError(pid)
        dados = atual.para_dict()
        dados.update({k: v for k, v in campos.items() if k in ("ativo", "tipo", "identificador", "quantidade", "preco_medio", "data_compra", "busca", "mandato")})
        dados.pop("mercado"), dados.pop("moeda")
        novo = Posicao(**{k: v for k, v in dados.items() if k != "ativa"})
        self._db.con.execute("UPDATE posicoes SET ativo=?, tipo=?, mercado=?, identificador=?, quantidade=?, preco_medio=?, moeda=?, data_compra=?, atualizada_em=?, busca=?, mandato=? WHERE id=?",
                             (novo.ativo, novo.tipo, novo.mercado, novo.identificador, novo.quantidade, novo.preco_medio,
                              novo.moeda, novo.data_compra.isoformat(), agora_utc().isoformat(), novo.busca or "", novo.mandato or "", pid))
        self._db.con.commit()
        return self.obter(pid)

    def excluir(self, pid: int) -> None:
        self._db.con.execute("UPDATE posicoes SET ativa = 0, atualizada_em = ? WHERE id = ?", (agora_utc().isoformat(), pid))
        self._db.con.commit()

    def definir_tese(self, pid: int, texto: str) -> None:
        self._db.con.execute("INSERT INTO teses (posicao_id, texto, criada_em) VALUES (?,?,?)", (pid, texto.strip(), agora_utc().isoformat()))
        self._db.con.commit()

    def tese(self, pid: int) -> str | None:
        r = self._db.con.execute("SELECT texto FROM teses WHERE posicao_id = ? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
        return r["texto"] if r else None

    def historico_teses(self, pid: int) -> list[dict]:
        return [dict(r) for r in self._db.con.execute("SELECT texto, criada_em FROM teses WHERE posicao_id = ? ORDER BY id", (pid,)).fetchall()]

    def importar_csv(self, caminho: str) -> int:
        """Colunas: ativo,tipo,identificador,quantidade,preco_medio,data_compra,tese. Erro aponta a linha."""
        n = 0
        with open(caminho, encoding="utf-8", newline="") as fh:
            for i, row in enumerate(csv.DictReader(fh), start=2):
                try:
                    p = Posicao(ativo=row.get("ativo", ""), tipo=row["tipo"].strip(), identificador=row["identificador"],
                                quantidade=float(row["quantidade"]), preco_medio=float(row["preco_medio"]),
                                data_compra=date.fromisoformat(row["data_compra"].strip()), busca=(row.get("busca") or "").strip(),
                                mandato=(row.get("mandato") or "").strip())
                except (KeyError, ValueError) as e:
                    raise ValueError(f"linha {i}: {e}") from e
                self.criar(p, row.get("tese") or None)
                n += 1
        return n

    # --- o que as fontes precisam saber ---
    def tickers(self) -> list[tuple[str, str]]:
        return sorted({(p.identificador, p.mercado) for p in self.listar() if p.mercado in ("B3", "US")})

    def cnpjs(self) -> list[str]:
        return sorted({p.identificador for p in self.listar() if p.tipo == "fundo"})


def valorizar(posicoes: list[Posicao], precos: dict[str, tuple[float, date]], cambio: float | None,
              hoje: date) -> pd.DataFrame:
    """`precos`: identificador → (preço na moeda do ativo, data do preço). Fundo = cota; Tesouro = PU.
    Posição sem preço fica na tabela com valor None — nunca some da carteira."""
    linhas = []
    for p in posicoes:
        preco, data_preco = precos.get(p.identificador, (None, None))
        fx = 1.0 if p.moeda == "BRL" else cambio
        custo_brl = p.quantidade * p.preco_medio * (fx or float("nan"))
        if preco is None or fx is None:
            valor_brl = pnl = pnl_pct = None
        else:
            valor_brl = p.quantidade * preco * fx
            pnl = valor_brl - p.quantidade * p.preco_medio * fx
            pnl_pct = (preco / p.preco_medio - 1) * 100
        linhas.append({"id": p.id, "ativo": p.ativo, "tipo": p.tipo, "mercado": p.mercado, "moeda": p.moeda,
                       "identificador": p.identificador, "quantidade": p.quantidade, "preco_medio": p.preco_medio,
                       "preco": preco, "data_preco": data_preco,
                       "idade_dias": (hoje - data_preco).days if data_preco else None,
                       "custo_brl": None if pd.isna(custo_brl) else custo_brl, "valor_brl": valor_brl,
                       "pnl_brl": pnl, "pnl_pct": pnl_pct, "data_compra": p.data_compra})
    df = pd.DataFrame(linhas)
    if df.empty:
        return df
    total = df["valor_brl"].dropna().sum()
    df["peso"] = df["valor_brl"] / total * 100 if total else None
    return df


def resumo(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"valor_brl": 0, "custo_brl": 0, "pnl_brl": 0, "por_moeda": {}, "por_tipo": {}, "sem_preco": []}
    v = df.dropna(subset=["valor_brl"])
    return {"valor_brl": float(v["valor_brl"].sum()), "custo_brl": float(v["custo_brl"].sum()),
            "pnl_brl": float(v["pnl_brl"].sum()),
            "por_moeda": {m: float(x) for m, x in v.groupby("moeda")["valor_brl"].sum().items()},
            "por_tipo": {t: float(x) for t, x in v.groupby("tipo")["valor_brl"].sum().items()},
            "sem_preco": df[df["valor_brl"].isna()]["ativo"].tolist()}
