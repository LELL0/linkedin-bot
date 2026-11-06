"""Runtime settings. Secrets come from .env, and all state lives outside the repository."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", Path.home() / ".local/share/linkedin-bot"))


@dataclass(frozen=True)
class Config:
    profile_dir: Path = DATA_DIR / "chrome-profile"
    db_path: Path = DATA_DIR / "state.db"
    log_path: Path = DATA_DIR / "bot.log"
    metrics_path: Path = DATA_DIR / "metrics.jsonl"
    reply_scope: str = os.environ.get("REPLY_SCOPE", "new_threads")
    dry_run: bool = os.environ.get("DRY_RUN", "0") == "1"
    # Any OpenAI-style chat API. The first model that answers is used.
    ai_base_url: str = os.environ.get("AI_BASE_URL", "https://api.groq.com/openai/v1")
    ai_api_key: str = os.environ.get("AI_API_KEY", "")
    ai_models: tuple[str, ...] = tuple(
        os.environ.get(
            "AI_MODELS",
            "qwen/qwen3.8-27b,openai/gpt-oss-120b,allam-2-7b",
        ).split(",")
    )
    # The first guard model stays out of ai_models, so answers can't use up its quota.
    # The others only step in when it is rate limited, so a check is rarely missed.
    ai_guard_models: tuple[str, ...] = tuple(
        os.environ.get(
            "AI_GUARD_MODELS", "openai/gpt-oss-20b,openai/gpt-oss-120b,qwen/qwen3.8-27b"
        ).split(",")
    )
    # Wait for this much silence before answering, so a burst gets one reply.
    debounce_quiet_s: float = 3.0
    debounce_max_s: float = 9.0
    # Backstop polling. The realtime wake-up is the fast path.
    idle_poll_s: float = float(os.environ.get("IDLE_POLL_S", "12"))
    # Invites get their own quick check, because accept speed is what matters there.
    invite_poll_s: float = float(os.environ.get("INVITE_POLL_S", "4"))
    hot_poll_s: float = 2.0
    hot_window_s: float = 90.0


CONFIG = Config()
