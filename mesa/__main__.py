"""`python -m mesa` sobe API + scheduler; `python -m mesa job manha|fechamento` roda um job e sai."""
import json
import logging
import sys

from mesa.config import Config, carregar_dotenv


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    carregar_dotenv()
    cfg = Config.do_ambiente()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if argv[:1] == ["job"]:
        from mesa import job
        nome = argv[1] if len(argv) > 1 else "manha"
        res = getattr(job, nome)(cfg)
        print(json.dumps(res, ensure_ascii=False, default=str, indent=1))
        return
    import uvicorn
    from mesa.api import criar_app, iniciar_scheduler
    app = criar_app(cfg)
    if cfg.scheduler:
        iniciar_scheduler(cfg)
    uvicorn.run(app, host="0.0.0.0", port=cfg.porta, log_level="info")


if __name__ == "__main__":
    main()
