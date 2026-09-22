"""Fundos de investimento (CVM, dados abertos): informe diário (cota, PL, captação, resgate,
cotistas), cadastro (classe, gestor, situação, taxas).

O informe diário vem em um zip por mês com TODOS os fundos (~30 MB, ~1,5 M linhas). Guardamos o zip
bruto em `dados/cvm/raw/` (só o mês corrente é rebaixado) e filtramos para os CNPJs da carteira mais
os pares: fundos da mesma classe com PL acima de um mínimo, que servem de comparação. Nomenclatura
nova da CVM (2025+): `CNPJ_FUNDO_CLASSE`, `TP_FUNDO_CLASSE`.
"""
import io
import os
import re
import zipfile
from datetime import date

import pandas as pd

from mesa.fontes import Coleta, Contexto, http_get

BASE = "https://dados.cvm.gov.br/dados/FI"
URL_INFORME = BASE + "/DOC/INF_DIARIO/DADOS/inf_diario_fi_{aaaamm}.zip"
URL_CADASTRO = BASE + "/CAD/DADOS/cad_fi.csv"
PL_MINIMO_PARES = 50_000_000
MAX_PARES_POR_CLASSE = 400


def so_digitos(cnpj: str) -> str:
    return re.sub(r"\D", "", cnpj or "")


def meses_ate(ref: date, n: int) -> list[str]:
    out, ano, mes = [], ref.year, ref.month
    for _ in range(n):
        out.append(f"{ano}{mes:02d}")
        mes -= 1
        if mes == 0:
            ano, mes = ano - 1, 12
    return list(reversed(out))


def parse_cadastro(texto: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(texto), sep=";", dtype=str, encoding_errors="ignore")
    col_cnpj = "CNPJ_FUNDO" if "CNPJ_FUNDO" in df.columns else "CNPJ_FUNDO_CLASSE"
    out = pd.DataFrame({
        "cnpj": df[col_cnpj].map(so_digitos),
        "nome": df["DENOM_SOCIAL"].str.strip(),
        "classe": df.get("CLASSE", pd.Series([""] * len(df))).fillna(""),
        "classe_anbima": df.get("CLASSE_ANBIMA", pd.Series([""] * len(df))).fillna(""),
        "gestor": df.get("GESTOR", pd.Series([""] * len(df))).fillna(""),
        "situacao": df.get("SIT", pd.Series([""] * len(df))).fillna(""),
        "publico_alvo": df.get("PUBLICO_ALVO", pd.Series([""] * len(df))).fillna(""),
        "taxa_adm": pd.to_numeric(df.get("TAXA_ADM"), errors="coerce"),
        "taxa_perf": pd.to_numeric(df.get("TAXA_PERFM"), errors="coerce"),
        "pl": pd.to_numeric(df.get("VL_PATRIM_LIQ"), errors="coerce"),
        "rentab_fundo": df.get("RENTAB_FUNDO", pd.Series([""] * len(df))).fillna(""),
    })
    return out[out["cnpj"].str.len() == 14].drop_duplicates("cnpj", keep="last")


def parse_informe(csv_bytes: bytes, cnpjs: set[str]) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(csv_bytes), sep=";", dtype=str, encoding="latin-1")
    col = "CNPJ_FUNDO_CLASSE" if "CNPJ_FUNDO_CLASSE" in df.columns else "CNPJ_FUNDO"
    df["cnpj"] = df[col].map(so_digitos)
    df = df[df["cnpj"].isin(cnpjs)]
    if df.empty:
        return pd.DataFrame()
    return pd.DataFrame({
        "cnpj": df["cnpj"].values,
        "data": pd.to_datetime(df["DT_COMPTC"]).dt.date.values,
        "cota": pd.to_numeric(df["VL_QUOTA"], errors="coerce").values,
        "pl": pd.to_numeric(df["VL_PATRIM_LIQ"], errors="coerce").values,
        "captacao": pd.to_numeric(df["CAPTC_DIA"], errors="coerce").values,
        "resgate": pd.to_numeric(df["RESG_DIA"], errors="coerce").values,
        "cotistas": pd.to_numeric(df["NR_COTST"], errors="coerce").values,
    }).dropna(subset=["cota"])


def escolher_pares(cadastro: pd.DataFrame, cnpjs: list[str]) -> set[str]:
    """Fundos ativos da mesma classe dos fundos da carteira, com PL relevante (limitado por classe)."""
    ativos = cadastro[cadastro["situacao"].str.upper().str.contains("FUNCIONAMENTO", na=False)]
    classes = set(cadastro[cadastro["cnpj"].isin(cnpjs)]["classe"]) - {""}
    pares = set()
    for classe in classes:
        cand = ativos[(ativos["classe"] == classe) & (ativos["pl"] >= PL_MINIMO_PARES)]
        cand = cand.sort_values("pl", ascending=False).head(MAX_PARES_POR_CLASSE)
        pares |= set(cand["cnpj"])
    return pares


class Cadastro:
    nome = "cvm_cadastro"
    tabela = "fundos_cadastro"

    def coletar(self, ctx: Contexto) -> tuple[pd.DataFrame, Coleta]:
        r = http_get(ctx)(URL_CADASTRO)
        df = parse_cadastro(r.content.decode("latin-1"))
        return df, Coleta(self.nome, True)


class InformeDiario:
    nome = "cvm_informe"
    tabela = "cotas_fundos"

    def __init__(self, cadastro_fn=None):
        self._cadastro_fn = cadastro_fn  # () -> DataFrame do cadastro (para escolher pares)

    def coletar(self, ctx: Contexto) -> tuple[pd.DataFrame, Coleta]:
        cnpjs = [so_digitos(c) for c in ctx.cnpjs]
        if not cnpjs:
            return pd.DataFrame(), Coleta(self.nome, True)
        alvo = set(cnpjs)
        if self._cadastro_fn is not None:
            cad = self._cadastro_fn()
            if cad is not None and not cad.empty:
                alvo |= escolher_pares(cad, cnpjs)
        pasta = os.path.join(ctx.dados_dir, "cvm", "raw")
        os.makedirs(pasta, exist_ok=True)
        get = http_get(ctx)
        partes, baixados, meses = [], 0, meses_ate(ctx.agora.date(), ctx.meses_cvm)
        for aaaamm in meses:
            caminho = os.path.join(pasta, f"inf_diario_fi_{aaaamm}.zip")
            corrente = aaaamm == meses[-1]
            if corrente or not os.path.exists(caminho):
                try:
                    r = get(URL_INFORME.format(aaaamm=aaaamm))
                except Exception as e:  # noqa: BLE001 - mes ausente (ex.: mes corrente ainda nao publicado)
                    if os.path.exists(caminho):
                        pass
                    else:
                        if corrente:
                            continue
                        raise
                else:
                    with open(caminho, "wb") as fh:
                        fh.write(r.content)
                    baixados += 1
            with zipfile.ZipFile(caminho) as zf:
                for nome in zf.namelist():
                    if nome.lower().endswith(".csv"):
                        partes.append(parse_informe(zf.read(nome), alvo))
        df = pd.concat([p for p in partes if not p.empty], ignore_index=True) if partes else pd.DataFrame()
        return df, Coleta(self.nome, True, detalhe={"meses": len(meses), "zips_baixados": baixados,
                                                    "fundos": int(df["cnpj"].nunique()) if not df.empty else 0,
                                                    "pares": len(alvo) - len(set(cnpjs))})
