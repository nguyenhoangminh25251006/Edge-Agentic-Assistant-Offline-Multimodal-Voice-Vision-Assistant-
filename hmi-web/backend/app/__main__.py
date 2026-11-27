"""Entry point: ``python -m app`` (run from ``hmi-web/backend``)."""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn

from .config import Settings


def main() -> None:
    cfg = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if sys.platform == "win32":
        # pyzmq's asyncio integration needs a selector loop (dev hosts only).
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
        loop = "none"
    else:
        loop = "auto"
    uvicorn.run(
        "app.main:app",
        host=cfg.host,
        port=cfg.port,
        log_level=cfg.log_level.lower(),
        loop=loop,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
