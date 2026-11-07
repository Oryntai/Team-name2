"""Запуск сервера: `python -m cqa` → http://127.0.0.1:8000 (порт — CQA_PORT, адрес — CQA_HOST).

Ключи и адреса читаются из окружения и из файла .env в корне проекта (в git не идёт):
ANTHROPIC_API_KEY, CQA_DATASET_URL (ссылка GET организаторов), CQA_LOCAL_BASE_URL / CQA_LOCAL_MODEL (своя модель).
База — var/cqa.db (CQA_DB). Пустая база и есть data/dev.json → набор DEV загружается сам.
"""
from __future__ import annotations

import logging
import os
import pathlib

import uvicorn

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_env(path: pathlib.Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> None:
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for name in ("httpx", "httpcore"):  # httpx пишет полный адрес запроса — там подпись ссылки API
        logging.getLogger(name).setLevel(logging.WARNING)
    from .api import create_app

    app = create_app(autoload_dev=True)
    host = os.environ.get("CQA_HOST", "127.0.0.1")
    port = int(os.environ.get("CQA_PORT", "8000"))
    print(f"CQA: http://{host}:{port}  (база: {app.state.db.path})", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
