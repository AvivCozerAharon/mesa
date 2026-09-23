"""Fundos de investimento (CVM, dados abertos): informe diário (cota, PL, captação, resgate,
cotistas), cadastro (classe, gestor, situação, taxas).

O informe diário vem em um zip por mês com TODOS os fundos (~30 MB, ~1,5 M linhas). Guardamos o zip
bruto em `dados/cvm/raw/` (só o mês corrente é rebaixado) e filtramos para os CNPJs da carteira mais
os pares: fundos da mesma classe com PL acima de um mínimo, que servem de comparação. Nomenclatura
nova da CVM (2025+): `CNPJ_FUNDO_CLASSE`, `TP_FUNDO_CLASSE`.
"""
import csv
import io
import os
import re
import zipfile
from datetime import date

import pandas as pd

from mesa.fontes import Coleta, Contexto, http_get

BASE = "https://dados.cvm.gov.br/dados/FI"
URL_INFORME = BASE + "/DOC/INF_DIARIO/DADOS/inf_diario_fi_{aaaamm}.zip"
URL_CADASTRO = BASE + "/CAD/DADOS/registro_fundo_classe.zip"
URL_LAMINA = BASE + "/DOC/LAMINA/DADOS/lamina_fi_{aaaamm}.zip"
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


def parse_cadastro(zip_bytes: bytes) -> pd.DataFrame:
    """registro_fundo_classe.zip (regime RCVM 175). O cad_fi.csv legado mostra 99 % dos fundos como
    CANCELADA desde a migração — quem usa ele acha que o mercado acabou."""
    ler = lambda nome: pd.read_csv(io.BytesIO(zipfile.ZipFile(io.BytesIO(zip_bytes)).read(nome)), sep=";", dtype=str,  # noqa: E731
                                   encoding="latin-1", quoting=csv.QUOTE_NONE, on_bad_lines="skip", engine="python")
    classe, fundo = ler("registro_classe.csv"), ler("registro_fundo.csv")
    try:
        subclasse = ler("registro_subclasse.csv")
    except Exception:  # noqa: BLE001 - zip antigo, sem o arquivo de subclasses
        subclasse = None
    gestor = fundo.drop_duplicates("ID_Registro_Fundo", keep="last").set_index("ID_Registro_Fundo")["Gestor"]
    out = pd.DataFrame({
        "cnpj": classe["CNPJ_Classe"].map(so_digitos),
        "nome": classe["Denominacao_Social"].fillna("").str.strip(),
        "classe": classe["Classificacao"].fillna(""),
        "tipo_classe": classe["Tipo_Classe"].fillna(""),
        "classe_anbima": classe["Classificacao_Anbima"].fillna(""),
        "situacao": classe["Situacao"].fillna(""),
        "publico_alvo": classe["Publico_Alvo"].fillna(""),
        "pl": pd.to_numeric(classe["Patrimonio_Liquido"], errors="coerce"),
        "data_pl": classe["Data_Patrimonio_Liquido"].fillna(""),
        "gestor": classe["ID_Registro_Fundo"].map(gestor).fillna(""),
    })
    out["subclasse"] = ""
    out = out[out["cnpj"].str.len() == 14].drop_duplicates("cnpj", keep="last")
    if subclasse is None or subclasse.empty:
        return out
    # Cada subclasse herda CNPJ, gestor e classe da sua classe; o que muda e o nome, a situacao e a taxa.
    por_classe = classe.drop_duplicates("ID_Registro_Classe", keep="last").set_index("ID_Registro_Classe")
    sub = pd.DataFrame({
        "cnpj": subclasse["ID_Registro_Classe"].map(por_classe["CNPJ_Classe"]).map(so_digitos),
        "subclasse": subclasse["ID_Subclasse"].fillna("").str.strip(),
        "nome": subclasse["Denominacao_Social"].fillna("").str.strip(),
        "nome_classe": subclasse["ID_Registro_Classe"].map(por_classe["Denominacao_Social"]).fillna("").str.strip(),
        "classe": subclasse["ID_Registro_Classe"].map(por_classe["Classificacao"]).fillna(""),
        "tipo_classe": subclasse["ID_Registro_Classe"].map(por_classe["Tipo_Classe"]).fillna(""),
        "classe_anbima": subclasse["ID_Registro_Classe"].map(por_classe["Classificacao_Anbima"]).fillna(""),
        "situacao": subclasse["Situacao"].fillna(""),
        "publico_alvo": subclasse["Publico_Alvo"].fillna(""),
        "pl": pd.NA,  # PL e publicado por classe, nao por subclasse
        "data_pl": "",
        "gestor": subclasse["ID_Registro_Classe"].map(por_classe["ID_Registro_Fundo"]).map(gestor).fillna(""),
    })
    # Muita subclasse se chama so "SUBCLASSE A": sozinho nao da para achar na busca nem entender na tela.
    curto = sub["nome"].str.len() < 25
    sub["nome"] = (sub["nome_classe"] + " — " + sub["nome"]).where(curto & (sub["nome_classe"] != ""), sub["nome"]).str.strip()
    sub = sub.drop(columns=["nome_classe"])
    sub = sub[(sub["cnpj"].str.len() == 14) & (sub["subclasse"] != "")].drop_duplicates(["cnpj", "subclasse"], keep="last")
    sub["pl"] = sub["cnpj"].map(out.set_index("cnpj")["pl"])  # PL da classe, para ordenar a busca
    return pd.concat([out, sub], ignore_index=True)


def parse_lamina(zip_bytes: bytes, data_ref: str) -> pd.DataFrame:
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    nome = next(n for n in z.namelist() if n.startswith("lamina_fi_") and "_" not in n[len("lamina_fi_"):])
    df = pd.read_csv(io.BytesIO(z.read(nome)), sep=";", dtype=str, encoding="latin-1", quoting=csv.QUOTE_NONE,
                     on_bad_lines="skip", engine="python")
    col = "CNPJ_FUNDO_CLASSE" if "CNPJ_FUNDO_CLASSE" in df.columns else "CNPJ_FUNDO"
    out = pd.DataFrame({"cnpj": df[col].map(so_digitos), "taxa_adm": pd.to_numeric(df.get("TAXA_ADM"), errors="coerce"),
                        "taxa_perf": pd.to_numeric(df.get("TAXA_PERFM"), errors="coerce"),
                        "benchmark_declarado": df.get("INDICE_REFER", pd.Series([""] * len(df))).fillna("").str.strip(),
                        "data_ref": data_ref})
    return out[out["cnpj"].str.len() == 14].drop_duplicates("cnpj", keep="last")


COLUNAS_INFORME = ["CNPJ_FUNDO_CLASSE", "CNPJ_FUNDO", "ID_SUBCLASSE", "DT_COMPTC", "VL_QUOTA",
                   "VL_PATRIM_LIQ", "CAPTC_DIA", "RESG_DIA", "NR_COTST"]


def parse_informe(csv_bytes: bytes, cnpjs: set[str]) -> pd.DataFrame:
    # Só as colunas usadas e com os números já parseados pelo C: o arquivo tem 350 mil linhas por mês
    # e são 36 meses por coleta.
    cabecalho = pd.read_csv(io.BytesIO(csv_bytes), sep=";", nrows=0, encoding="latin-1").columns
    usar = [c for c in COLUNAS_INFORME if c in cabecalho]
    texto = {c: "string" for c in ("CNPJ_FUNDO_CLASSE", "CNPJ_FUNDO", "ID_SUBCLASSE", "DT_COMPTC") if c in usar}
    df = pd.read_csv(io.BytesIO(csv_bytes), sep=";", encoding="latin-1", usecols=usar, dtype=texto)
    col = "CNPJ_FUNDO_CLASSE" if "CNPJ_FUNDO_CLASSE" in df.columns else "CNPJ_FUNDO"
    df["cnpj"] = df[col].map(so_digitos)
    df = df[df["cnpj"].isin(cnpjs)]
    if df.empty:
        return pd.DataFrame()
    sub = df["ID_SUBCLASSE"].fillna("").str.strip() if "ID_SUBCLASSE" in df.columns else ""
    return pd.DataFrame({
        "cnpj": df["cnpj"].values,
        "subclasse": sub.values if hasattr(sub, "values") else sub,
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
    if "publico_alvo" in ativos.columns:  # pares comparaveis: mesmo publico (geral vs qualificado) quando informado
        pass
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
        df = parse_cadastro(r.content)
        return df, Coleta(self.nome, True)


class Lamina:
    """Taxas e benchmark declarado: ultimas 12 laminas mensais, fica a mais recente por fundo."""
    nome = "cvm_lamina"
    tabela = "fundos_taxas"

    def coletar(self, ctx: Contexto) -> tuple[pd.DataFrame, Coleta]:
        get = http_get(ctx)
        partes = []
        for aaaamm in meses_ate(ctx.agora.date(), 12):
            try:
                partes.append(parse_lamina(get(URL_LAMINA.format(aaaamm=aaaamm)).content, aaaamm))
            except Exception:  # noqa: BLE001 - mes sem lamina publicada
                continue
        if not partes:
            return pd.DataFrame(), Coleta(self.nome, False, erro="nenhuma lamina disponivel")
        df = pd.concat(partes, ignore_index=True).sort_values("data_ref").drop_duplicates("cnpj", keep="last")
        return df, Coleta(self.nome, True, detalhe={"meses": len(partes)})


class InformeDiario:
    nome = "cvm_informe"
    tabela = "cotas_fundos"

    def __init__(self, cadastro_fn=None, meses: int | None = None):
        self._cadastro_fn = cadastro_fn  # () -> DataFrame do cadastro (para escolher pares)
        self._meses = meses              # menos meses = coleta sob demanda mais rapida

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
        partes, baixados, meses = [], 0, meses_ate(ctx.agora.date(), self._meses or ctx.meses_cvm)
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
        partes = [p for p in partes if not p.empty]
        df = pd.concat(partes, ignore_index=True) if partes else pd.DataFrame()
        return df, Coleta(self.nome, True, detalhe={"meses": len(meses), "zips_baixados": baixados,
                                                    "fundos": int(df["cnpj"].nunique()) if not df.empty else 0,
                                                    "pares": len(alvo) - len(set(cnpjs))})
