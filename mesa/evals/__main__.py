"""python -m mesa.evals [--com-ia] -> imprime tabela e grava docs/evals.json."""
import argparse
import json
import sys
from pathlib import Path

from mesa.armazenamento import Db
from mesa.config import Config, carregar_dotenv
from mesa.evals import registrar, rodar_casamento, rodar_tese, rodar_validacao


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--com-ia", action="store_true", help="roda tambem os casos de tese (usa a chave da OpenAI)")
    p.add_argument("--saida", default="docs/evals.json")
    a = p.parse_args(argv)
    carregar_dotenv()
    cfg = Config.do_ambiente()
    db = Db(cfg.db_path)
    res = {"casamento": rodar_casamento(), "validacao": rodar_validacao()}
    if a.com_ia:
        from mesa.ia import ClienteOpenAI
        res["tese"] = rodar_tese(ClienteOpenAI(cfg), Db(":memory:"), cfg)
    for suite, r in res.items():
        registrar(db, suite, r)
    Path(a.saida).parent.mkdir(parents=True, exist_ok=True)
    Path(a.saida).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    c, v = res["casamento"], res["validacao"]
    print(f"casamento noticia<->ativo: n={c['n']} precisao={c['precisao']:.0%} recall={c['recall']:.0%}")
    for e in c["erros"]:
        print("   ", e)
    print(f"validador da saida da IA: {v['acertos']}/{v['n']} casos corretos")
    for e in v["erros"]:
        print("   ", e)
    if "tese" in res:
        t = res["tese"]
        print(f"tese continua de pe (IA): concordancia {t['concordancia']:.0%} em {t['n']} casos; validos {t['validos']}/{t['n']}")
        for d in t["detalhes"]:
            print("   ", d)
    print(f"gravado em {a.saida}")


if __name__ == "__main__":
    main()
