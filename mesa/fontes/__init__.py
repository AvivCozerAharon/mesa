"""Adapters de fonte. Contrato único: `coletar(ctx) -> (DataFrame, Coleta)`; nunca lança para o
job — erro vira `Coleta(ok=False, erro=...)` e o DataFrame vem vazio. O job grava o Parquet e o
registro em `coletas`.

`ctx` é um `Contexto`: o que a fonte precisa saber da carteira (tickers, CNPJs), o "agora" com
fuso, quantos anos/meses de histórico e onde ficam os caches brutos.
"""
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Protocol

import pandas as pd

UA = {"User-Agent": "Mozilla/5.0 (mesa; +https://github.com/AvivCozerAharon)"}


@dataclass
class Coleta:
    fonte: str
    ok: bool
    linhas: int = 0
    duracao_ms: int = 0
    erro: str | None = None
    detalhe: dict = field(default_factory=dict)


@dataclass
class Contexto:
    agora: datetime                       # com fuso
    tickers: list[tuple[str, str]]        # (ativo, mercado) ex.: ("PETR4","B3"), ("AAPL","US")
    cnpjs: list[str]                      # fundos da carteira (só dígitos)
    dados_dir: str = "dados"
    anos_precos: int = 5
    meses_cvm: int = 36
    http_get: Callable | None = None      # injetável nos testes


class Fonte(Protocol):
    nome: str
    tabela: str                           # view do DuckDB onde o resultado entra

    def coletar(self, ctx: Contexto) -> tuple[pd.DataFrame, Coleta]: ...


def executar(fonte: "Fonte", ctx: Contexto) -> tuple[pd.DataFrame, Coleta]:
    """Envelope que mede tempo e transforma exceção em Coleta(ok=False)."""
    inicio = time.time()
    try:
        df, coleta = fonte.coletar(ctx)
    except Exception as e:  # noqa: BLE001 - qualquer falha de fonte vira registro, nunca derruba o job
        df, coleta = pd.DataFrame(), Coleta(fonte.nome, False, erro=f"{type(e).__name__}: {e}"[:500])
    coleta.duracao_ms = int((time.time() - inicio) * 1000)
    coleta.linhas = len(df)
    return df, coleta


def http_get(ctx: Contexto):
    if ctx.http_get is not None:
        return ctx.http_get
    import requests

    def _get(url, **kw):
        kw.setdefault("headers", UA)
        kw.setdefault("timeout", 120)
        r = requests.get(url, **kw)
        r.raise_for_status()
        return r
    return _get
