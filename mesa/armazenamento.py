"""Armazenamento: Parquet imutável por (fonte, data da coleta) lido pelo DuckDB; SQLite para o
transacional (carteira, teses, coletas, métricas, briefings).

Cada coleta grava `dados/<fonte>/<AAAA-MM-DD>.parquet`. Nada é apagado: reexecutar o mesmo dia
sobrescreve só aquele arquivo. As views do DuckDB deduplicam pela chave natural ficando com a
coleta mais recente, então um preço corrigido pela fonte substitui o anterior sem apagar história.
"""
import json
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

# fonte -> (view, chave natural)
VIEWS = {
    "precos": ("precos", ["ativo", "data"]),
    "cotas_fundos": ("cotas_fundos", ["cnpj", "data"]),
    "benchmarks": ("benchmarks", ["nome", "data"]),
    "curvas": ("curvas", ["pais", "data", "titulo", "vencimento"]),
    "fundos_cadastro": ("fundos_cadastro", ["cnpj"]),
    "fundos_taxas": ("fundos_taxas", ["cnpj"]),
    "fundamentos": ("fundamentos", ["ativo"]),
    "dre_trimestral": ("dre_trimestral", ["ativo", "trimestre"]),
}


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def gravar_parquet(dados_dir: str, tabela: str, data_coleta: date, df: pd.DataFrame, fonte: str | None = None) -> Path:
    """Arquivo por (tabela, fonte, data): duas fontes na mesma tabela (Tesouro e Treasury em `curvas`)
    não podem sobrescrever uma à outra — foi o primeiro bug de produção."""
    pasta = Path(dados_dir) / tabela
    pasta.mkdir(parents=True, exist_ok=True)
    prefixo = f"{fonte}_" if fonte else ""
    destino = pasta / f"{prefixo}{data_coleta.isoformat()}.parquet"
    df = df.copy()
    if "coletado_em" not in df.columns:
        df["coletado_em"] = agora_utc()
    df.to_parquet(destino, index=False)
    return destino


class Consulta:
    """Consultas analíticas sobre os Parquet (DuckDB em memória, views recriadas a cada conexão)."""

    def __init__(self, dados_dir: str = "dados"):
        self.dados_dir = Path(dados_dir)

    def duck(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        for fonte, (view, chave) in VIEWS.items():
            pasta = self.dados_dir / fonte
            if not pasta.exists() or not any(pasta.glob("*.parquet")):
                continue
            particao = ", ".join(chave)
            con.execute(f"""
                CREATE VIEW {view} AS
                SELECT * FROM read_parquet('{pasta.as_posix()}/*.parquet', union_by_name=true)
                QUALIFY row_number() OVER (PARTITION BY {particao} ORDER BY coletado_em DESC) = 1
            """)
        return con

    def _df(self, sql: str, params: list) -> pd.DataFrame:
        con = self.duck()
        try:
            existentes = {r[0] for r in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()}
            for view, _ in VIEWS.values():
                if view not in existentes and f" {view} " in f" {sql} ".replace("\n", " "):
                    return pd.DataFrame()
            return con.execute(sql, params).df()
        finally:
            con.close()

    def precos(self, ativos: list[str], inicio: date | None = None, fim: date | None = None) -> pd.DataFrame:
        if not ativos:
            return pd.DataFrame()
        marcas = ", ".join("?" for _ in ativos)
        return self._df(f"SELECT * FROM precos WHERE ativo IN ({marcas}) AND data >= ? AND data <= ? ORDER BY ativo, data",
                        [*ativos, inicio or date(1900, 1, 1), fim or date(2100, 1, 1)])

    def benchmark(self, nome: str, inicio: date | None = None, fim: date | None = None) -> pd.Series:
        df = self._df("SELECT data, valor FROM benchmarks WHERE nome = ? AND data >= ? AND data <= ? ORDER BY data",
                      [nome, inicio or date(1900, 1, 1), fim or date(2100, 1, 1)])
        if df.empty:
            return pd.Series(dtype=float)
        return pd.Series(df["valor"].values, index=pd.to_datetime(df["data"]), name=nome)

    def cotas(self, cnpjs: list[str], inicio: date | None = None, fim: date | None = None) -> pd.DataFrame:
        if not cnpjs:
            return pd.DataFrame()
        marcas = ", ".join("?" for _ in cnpjs)
        return self._df(f"SELECT * FROM cotas_fundos WHERE cnpj IN ({marcas}) AND data >= ? AND data <= ? ORDER BY cnpj, data",
                        [*cnpjs, inicio or date(1900, 1, 1), fim or date(2100, 1, 1)])

    def curvas(self, pais: str, datas: list[date]) -> pd.DataFrame:
        if not datas:
            return pd.DataFrame()
        marcas = ", ".join("?" for _ in datas)
        return self._df(f"SELECT * FROM curvas WHERE pais = ? AND data IN ({marcas}) ORDER BY data, vencimento", [pais, *datas])

    def cadastro_fundos(self, cnpjs: list[str] | None = None) -> pd.DataFrame:
        if cnpjs is None:
            return self._df("SELECT * FROM fundos_cadastro", [])
        if not cnpjs:
            return pd.DataFrame()
        marcas = ", ".join("?" for _ in cnpjs)
        return self._df(f"SELECT * FROM fundos_cadastro WHERE cnpj IN ({marcas})", cnpjs)

    def taxas_fundos(self, cnpjs: list[str]) -> pd.DataFrame:
        if not cnpjs:
            return pd.DataFrame()
        marcas = ", ".join("?" for _ in cnpjs)
        return self._df(f"SELECT * FROM fundos_taxas WHERE cnpj IN ({marcas})", cnpjs)

    def fundamentos(self, ativos: list[str]) -> pd.DataFrame:
        if not ativos:
            return pd.DataFrame()
        marcas = ", ".join("?" for _ in ativos)
        return self._df(f"SELECT * FROM fundamentos WHERE ativo IN ({marcas})", ativos)

    def dre(self, ativos: list[str]) -> pd.DataFrame:
        if not ativos:
            return pd.DataFrame()
        marcas = ", ".join("?" for _ in ativos)
        return self._df(f"SELECT * FROM dre_trimestral WHERE ativo IN ({marcas}) ORDER BY ativo, trimestre", ativos)

    def ultima_data(self, view: str, coluna_chave: str, valor: str) -> date | None:
        df = self._df(f"SELECT max(data) AS d FROM {view} WHERE {coluna_chave} = ?", [valor])
        if df.empty or pd.isna(df["d"].iloc[0]):
            return None
        return pd.Timestamp(df["d"].iloc[0]).date()


SCHEMA = """
CREATE TABLE IF NOT EXISTS posicoes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ativo TEXT NOT NULL, tipo TEXT NOT NULL, mercado TEXT NOT NULL,
  identificador TEXT NOT NULL, quantidade REAL NOT NULL, preco_medio REAL NOT NULL, moeda TEXT NOT NULL,
  data_compra TEXT NOT NULL, ativa INTEGER NOT NULL DEFAULT 1, criada_em TEXT NOT NULL, atualizada_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS compras (
  id INTEGER PRIMARY KEY AUTOINCREMENT, posicao_id INTEGER NOT NULL, data TEXT NOT NULL,
  quantidade REAL NOT NULL, preco REAL NOT NULL, criada_em TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_compras_posicao ON compras (posicao_id);
CREATE TABLE IF NOT EXISTS teses (
  id INTEGER PRIMARY KEY AUTOINCREMENT, posicao_id INTEGER NOT NULL, texto TEXT NOT NULL, criada_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gatilhos (
  id INTEGER PRIMARY KEY AUTOINCREMENT, posicao_id INTEGER, regra TEXT NOT NULL, parametros TEXT NOT NULL,
  ativo INTEGER NOT NULL DEFAULT 1, criado_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS disparos (
  id INTEGER PRIMARY KEY AUTOINCREMENT, gatilho_id INTEGER NOT NULL, posicao_id INTEGER, data TEXT NOT NULL,
  estado_hash TEXT NOT NULL, detalhe TEXT NOT NULL, visto INTEGER NOT NULL DEFAULT 0, UNIQUE(gatilho_id, estado_hash));
CREATE TABLE IF NOT EXISTS noticias (
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, titulo TEXT NOT NULL, fonte TEXT,
  publicada_em TEXT, coletada_em TEXT NOT NULL, titulo_norm TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS noticia_ativo (noticia_id INTEGER NOT NULL, ativo TEXT NOT NULL, metodo TEXT, score REAL,
  UNIQUE(noticia_id, ativo));
CREATE TABLE IF NOT EXISTS briefings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL, posicao_id INTEGER, entrada TEXT NOT NULL,
  saida TEXT, modelo TEXT, tokens_in INTEGER, tokens_out INTEGER, custo_usd REAL, latencia_ms INTEGER,
  valido INTEGER, erro TEXT, criado_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coletas (
  id INTEGER PRIMARY KEY AUTOINCREMENT, fonte TEXT NOT NULL, data_ref TEXT NOT NULL, linhas INTEGER,
  duracao_ms INTEGER, erro TEXT, executada_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metricas_diarias (
  data TEXT NOT NULL, posicao_id INTEGER NOT NULL, json TEXT NOT NULL, calculada_em TEXT NOT NULL,
  PRIMARY KEY (data, posicao_id));
CREATE TABLE IF NOT EXISTS evals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL, suite TEXT NOT NULL, casos INTEGER, acertos INTEGER, detalhe TEXT);
"""


class Db:
    def __init__(self, db_path: str = "dados/mesa.db"):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.path = db_path
        self._con = sqlite3.connect(db_path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.executescript(SCHEMA)
        self._migrar()

    def _migrar(self) -> None:
        """Colunas adicionadas depois do schema inicial (SQLite nao tem ADD COLUMN IF NOT EXISTS)."""
        colunas = {r[1] for r in self._con.execute("PRAGMA table_info(posicoes)").fetchall()}
        if "busca" not in colunas:
            self._con.execute("ALTER TABLE posicoes ADD COLUMN busca TEXT NOT NULL DEFAULT ''")
        if "mandato" not in colunas:
            self._con.execute("ALTER TABLE posicoes ADD COLUMN mandato TEXT NOT NULL DEFAULT ''")
        # Posicao existente vira a sua primeira compra, para que quantidade e preco medio passem a ser
        # derivados das compras sem que nenhuma carteira antiga perca o que ja estava lancado.
        self._con.execute("INSERT INTO compras (posicao_id, data, quantidade, preco, criada_em)"
                          " SELECT id, data_compra, quantidade, preco_medio, criada_em FROM posicoes p"
                          " WHERE NOT EXISTS (SELECT 1 FROM compras c WHERE c.posicao_id = p.id)")
        self._con.commit()

    @property
    def con(self) -> sqlite3.Connection:
        return self._con

    def registrar_coleta(self, fonte: str, data_ref: date, linhas: int | None, duracao_ms: int, erro: str | None = None) -> None:
        self._con.execute("INSERT INTO coletas (fonte, data_ref, linhas, duracao_ms, erro, executada_em) VALUES (?,?,?,?,?,?)",
                          (fonte, data_ref.isoformat(), linhas, duracao_ms, erro, agora_utc().isoformat()))
        self._con.commit()

    def ultimas_coletas(self, limite: int = 50) -> list[dict]:
        rows = self._con.execute("SELECT * FROM coletas ORDER BY id DESC LIMIT ?", (limite,)).fetchall()
        return [dict(r) for r in rows]

    def gravar_metricas(self, data: date, posicao_id: int, metricas: dict) -> None:
        self._con.execute("INSERT OR REPLACE INTO metricas_diarias VALUES (?,?,?,?)",
                          (data.isoformat(), posicao_id, json.dumps(metricas, ensure_ascii=False, default=str),
                           agora_utc().isoformat()))
        self._con.commit()

    def metricas_recentes(self) -> dict[int, dict]:
        rows = self._con.execute("""SELECT posicao_id, json, data FROM metricas_diarias m
                                    WHERE data = (SELECT max(data) FROM metricas_diarias WHERE posicao_id = m.posicao_id)""").fetchall()
        return {r["posicao_id"]: {**json.loads(r["json"]), "data_metricas": r["data"]} for r in rows}
