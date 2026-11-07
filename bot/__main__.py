"""Entry point.

python -m bot login    open the sign-in window
python -m bot doctor   read-only check of every endpoint; prints no names or message text
python -m bot run      start the bot
python -m bot stats    summarise accept and reply timings
python -m bot health   exit 0 if healthy, 1 if not (Docker HEALTHCHECK)
"""

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import CONFIG
from .doctor import doctor
from .engine import Engine
from .health import check
from .metrics import print_stats
from .session import login


def setup_logging() -> None:
    CONFIG.log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(CONFIG.log_path, maxBytes=5_000_000, backupCount=2),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # /verbose 5 makes the root logger keep debug records; the console and file stay at INFO.
    for handler in logging.getLogger().handlers:
        handler.setLevel(logging.INFO)


def main() -> None:
    setup_logging()
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "login":
        asyncio.run(login())
    elif command == "doctor":
        asyncio.run(doctor())
    elif command == "health":
        sys.exit(check())
    elif command == "stats":
        print_stats()
    elif command == "run":
        asyncio.run(Engine().run())
    else:
        raise SystemExit(f"Unknown command: {command}")


if __name__ == "__main__":
    main()
