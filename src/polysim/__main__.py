"""Start the Discord bot."""

from __future__ import annotations

import asyncio
import logging

from .bot import run
from .config import Settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
