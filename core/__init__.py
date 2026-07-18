try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*_args, **_kwargs):
        return False

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def telegram_disabled() -> bool:
    if _truthy(os.environ.get("CLAUDE_STACK_DISABLE_TELEGRAM")):
        return True
    if str(os.environ.get("CLAUDE_STACK_TELEGRAM_ALERTS") or "").strip().lower() in {"0", "false", "no", "off"}:
        return True
    try:
        import json

        path = BASE_DIR / "data" / "runtime" / "telegram_disabled.json"
        if path.exists():
            payload = json.loads(path.read_text())
            return bool(payload.get("active", True))
    except Exception:
        pass
    return False


if telegram_disabled():
    for _key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "HUSTLE_TELEGRAM_BOT_TOKEN",
        "HUSTLE_TELEGRAM_CHAT_ID",
    ):
        os.environ.pop(_key, None)

with open(BASE_DIR / "config" / "settings.toml", "rb") as f:
    SETTINGS = tomllib.load(f)

with open(BASE_DIR / "config" / "branding.toml", "rb") as f:
    BRANDING = tomllib.load(f)

with open(BASE_DIR / "config" / "users.toml", "rb") as f:
    USERS = tomllib.load(f)


# -- Shared timezone helper --
DEFAULT_STACK_TIMEZONE = "America/Chicago"
STACK_TZ = None


def _stack_timezone_name() -> str:
    return (
        os.environ.get("CLAUDE_STACK_TIMEZONE")
        or os.environ.get("TZ")
        or str((SETTINGS.get("system") or {}).get("timezone") or "").strip()
        or DEFAULT_STACK_TIMEZONE
    )


def _get_stack_tz():
    """Lazy-load the configured stack timezone."""
    global STACK_TZ
    if STACK_TZ is None:
        import zoneinfo
        STACK_TZ = zoneinfo.ZoneInfo(_stack_timezone_name())
    return STACK_TZ


def mountain_now():
    """Legacy helper name; returns current time in the configured stack timezone."""
    from datetime import datetime
    return datetime.now(_get_stack_tz())


MOUNTAIN_TZ = None
def _get_mountain_tz():
    """Back-compat alias for the configured stack timezone."""
    global MOUNTAIN_TZ
    if MOUNTAIN_TZ is None:
        MOUNTAIN_TZ = _get_stack_tz()
    return MOUNTAIN_TZ
