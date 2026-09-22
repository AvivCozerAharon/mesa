"""Configuração por variáveis de ambiente (arquivo .env carregado se existir)."""
import os
from dataclasses import dataclass, field, fields
from typing import Mapping


@dataclass(frozen=True)
class Config:
    dados_dir: str = "dados"
    db_path: str = "dados/mesa.db"
    porta: int = 8100
    senha: str = ""                      # MESA_SENHA: vazio = sem login (só para dev local)
    openai_api_key: str = ""
    openai_modelo: str = "gpt-5-mini"
    fuso: str = "America/Sao_Paulo"
    smtp_host: str = ""
    smtp_porta: int = 587
    smtp_usuario: str = ""
    smtp_senha: str = ""
    email_destino: str = ""
    meses_cvm: int = 36                  # histórico de informes diários de fundos
    anos_precos: int = 5
    hora_manha: str = "07:00"            # jobs em America/Sao_Paulo
    hora_fechamento_b3: str = "18:40"
    hora_fechamento_us: str = "17:10"    # em America/New_York
    scheduler: bool = True

    # nomes de env: MESA_<CAMPO> tem prioridade; OPENAI_API_KEY é aceito sem prefixo
    @classmethod
    def do_ambiente(cls, env: Mapping[str, str] = os.environ) -> "Config":
        valores = {}
        for f in fields(cls):
            bruto = env.get(f"MESA_{f.name.upper()}")
            if bruto is None and f.name == "openai_api_key":
                bruto = env.get("OPENAI_API_KEY")
            if bruto is None or bruto == "":
                continue
            if f.type is int:
                valores[f.name] = int(bruto)
            elif f.type is bool:
                valores[f.name] = bruto.strip().lower() in ("1", "true", "sim", "yes")
            else:
                valores[f.name] = bruto
        return cls(**valores)


def carregar_dotenv(caminho: str = ".env") -> None:
    if os.path.exists(caminho):
        from dotenv import load_dotenv
        load_dotenv(caminho)
