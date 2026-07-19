#!/usr/bin/env python3
"""Entrypoint: `python agent-server.py`. Never imported — logic lives in server.py."""

import uvicorn

from config import load
from server import create_app


def main() -> None:
    settings = load()
    settings.users_root.mkdir(parents=True, exist_ok=True)
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="debug" if settings.debug else "info")


if __name__ == "__main__":
    main()
