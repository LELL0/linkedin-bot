"""Runtime settings. Secrets come from .env, and all state lives outside the repository."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

def _range(name: str, default: str) -> tuple[float, float]:
    """"a-b" from the environment as (a, b)."""
    low, _, high = os.environ.get(name, default).partition("-")
    return float(low), float(high or low)


DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", Path.home() / ".local/share/linkedin-bot"))


@dataclass(frozen=True)
class Config:
    profile_dir: Path = DATA_DIR / "chrome-profile"
    db_path: Path = DATA_DIR / "state.db"
    log_path: Path = DATA_DIR / "bot.log"
    metrics_path: Path = DATA_DIR / "metrics.jsonl"
    status_path: Path = DATA_DIR / "status.json"
    # Facts the bot may state about the owner. Private: kept with the data, not in the repo.
    profile_path: Path = Path(os.environ.get("BOT_PROFILE", DATA_DIR / "profile.md"))
    # "chrome" uses installed Google Chrome; "chromium" uses Playwright's own Chromium (servers, arm64).
    browser_channel: str = os.environ.get("BOT_BROWSER_CHANNEL", "chrome")
    chromium_sandbox: bool = os.environ.get("BOT_CHROMIUM_SANDBOX", "0") == "1"
    reply_scope: str = os.environ.get("REPLY_SCOPE", "new_threads")
    dry_run: bool = os.environ.get("DRY_RUN", "0") == "1"
    # Any OpenAI-style chat API. The first model that answers is used.
    ai_base_url: str = os.environ.get("AI_BASE_URL", "https://api.groq.com/openai/v1")
    ai_api_key: str = os.environ.get("AI_API_KEY", "")
    ai_models: tuple[str, ...] = tuple(
        os.environ.get(
            "AI_MODELS",
            "qwen/qwen3.8-27b,openai/gpt-oss-120b",
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
    # Backstop polling. The realtime wake-up is the fast path. LinkedIn watches request
    # rates per account: a check every few seconds, all day, is what gets a session
    # signed out and the account restricted, so these stay slow on purpose.
    idle_poll_s: float = float(os.environ.get("IDLE_POLL_S", "120"))
    invite_poll_s: float = float(os.environ.get("INVITE_POLL_S", "900"))
    hot_poll_s: float = 20.0
    hot_window_s: float = 300.0
    # Never check the inbox more often than this, even when realtime events pour in.
    min_check_gap_s: float = float(os.environ.get("MIN_CHECK_GAP_S", "15"))
    # Local hours (start-end, 24h clock) when the bot works. Outside them it makes no
    # requests at all, like a person who is asleep; what arrives is answered in the morning.
    active_hours: tuple[float, float] = _range("ACTIVE_HOURS", "8-24")
    # A person does not accept an invite or answer a message within seconds, every time.
    accept_delay_s: tuple[float, float] = _range("ACCEPT_DELAY_S", "180-2400")
    reply_delay_s: tuple[float, float] = _range("REPLY_DELAY_S", "25-150")
    max_accepts_per_day: int = int(os.environ.get("MAX_ACCEPTS_PER_DAY", "20"))
    max_replies_per_day: int = int(os.environ.get("MAX_REPLIES_PER_DAY", "40"))
    # Waits after LinkedIn pushes back (429, 999, 5xx) or rejects the session, doubling
    # each time up to the maximum.
    backoff_s: float = 300.0
    max_backoff_s: float = 4 * 3600.0

CONFIG = Config()
