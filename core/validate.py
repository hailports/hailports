"""Startup config validation — fail fast with clear errors."""

import os
import sys
import logging

log = logging.getLogger(__name__)

REQUIRED_KEYS = {
    "OPENROUTER_API_KEY": "OpenRouter API key",
}

OPTIONAL_KEYS = {
    "TELEGRAM_BOT_TOKEN": "Telegram bot (frontends.telegram_bot)",
    "SALESFORCE_CONSUMER_KEY": "Salesforce (tools.salesforce)",
    "SALESFORCE_USERNAME": "Salesforce (tools.salesforce)",
    "MONDAY_API_TOKEN": "Monday.com (tools.monday)",
    "SLACK_BOT_TOKEN": "Slack (tools.slack)",
    "SLACK_CLIENT_ID": "Slack OAuth refresh (auth.slack_auth)",
    "SLACK_CLIENT_SECRET": "Slack OAuth refresh (auth.slack_auth)",
    "ZOOM_ACCOUNT_ID": "Zoom (tools.zoom)",
    "ZOOM_CLIENT_ID": "Zoom (tools.zoom)",
    "ZOOM_CLIENT_SECRET": "Zoom (tools.zoom)",
    "GOOGLE_CLIENT_ID": "Google Drive (tools.google_drive)",
    "GOOGLE_CLIENT_SECRET": "Google Drive (tools.google_drive)",
    "WEBUI_API_KEY": "WebUI bridge (frontends.webui_bridge)",
}


def _env_truthy(key: str) -> bool:
    return os.environ.get(key, "").strip().lower() in {"1", "true", "yes", "on"}


def _paid_api_required() -> bool:
    if _env_truthy("CLAUDE_STACK_ENABLE_PAID_LLM_API"):
        return True
    if (
        _env_truthy("CLAUDE_STACK_FORCE_LOCAL_ROUTING")
        or _env_truthy("CLAUDE_STACK_FORCE_LOCAL_ROUTING_HARD")
        or _env_truthy("FORCE_LOCAL_LLM")
    ):
        return False
    return True


def validate_env(require_all=False):
    """Check that required env vars are set. Warns about missing optional ones."""
    missing_required = []
    missing_optional = []

    for key, desc in REQUIRED_KEYS.items():
        if key == "OPENROUTER_API_KEY" and not _paid_api_required():
            continue
        if not os.environ.get(key):
            missing_required.append(f"  {key} — {desc}")

    for key, desc in OPTIONAL_KEYS.items():
        if not os.environ.get(key):
            missing_optional.append(f"  {key} — {desc}")

    if missing_required:
        log.error("Missing REQUIRED environment variables:\n" + "\n".join(missing_required))
        log.error("Set these in .env")
        sys.exit(1)

    if missing_optional:
        log.info("Missing optional env vars (features disabled):\n" + "\n".join(missing_optional))

    log.info("Config validation passed.")
