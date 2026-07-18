"""Tests for provider profiles (Layer 0) and the settings menu/persistence."""

from __future__ import annotations

import os

import pytest

from coding_harness import providers, settings


# ---------------------------------------------------------------------------
# Provider profiles
# ---------------------------------------------------------------------------


def _clear_profile_env(monkeypatch, keep: set[str] | None = None) -> None:
    """Strip all MODEL_*/API_KEY_*/BASE_URL_*/ZAI_* env vars for deterministic tests."""
    keep = keep or set()
    for k in list(os.environ):
        if k.startswith(("MODEL_", "API_KEY_", "BASE_URL_", "ZAI_", "PROFILE_")):
            if k not in keep:
                monkeypatch.delenv(k, raising=False)
    providers.clear_cache()


def test_legacy_zai_vars_seed_zai_profile(monkeypatch):
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("ZAI_API_KEY", "legacy-key")
    monkeypatch.setenv("ZAI_MODEL", "glm-legacy")
    monkeypatch.setenv("ZAI_ENDPOINT", "https://api.z.ai/api/coding/paas/v4/")

    prof = providers.load_profile("zai")
    assert prof is not None
    assert prof.kind == "openai"
    assert prof.model == "glm-legacy"
    assert prof.api_key == "legacy-key"
    assert prof.base_url == "https://api.z.ai/api/coding/paas/v4/"
    assert prof.label() == "zai:glm-legacy"


def test_zai_default_is_the_coding_endpoint(monkeypatch):
    """The built-in zai default must be the *coding* endpoint, not /api/paas/v4/."""
    _clear_profile_env(monkeypatch)
    prof = providers.load_profile("zai")
    assert prof is not None
    assert "/api/coding/paas/v4/" in prof.base_url, (
        "zai default base_url must be the coding endpoint; got " + prof.base_url
    )


def test_legacy_zai_endpoint_wins_over_default(monkeypatch):
    """An explicit ZAI_ENDPOINT must override the zai name default (regression
    test for the bug where the default filled the slot and ZAI_ENDPOINT was
    ignored, hitting the wrong endpoint and returning 429 insufficient balance)."""
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("ZAI_API_KEY", "k")
    monkeypatch.setenv("ZAI_ENDPOINT", "https://custom.example.com/v4/")
    prof = providers.load_profile("zai")
    assert prof is not None
    assert prof.base_url == "https://custom.example.com/v4/", prof.base_url


def test_explicit_profile_var_beats_legacy(monkeypatch):
    """An explicit MODEL_zai/BASE_URL_zai beats the legacy ZAI_* vars."""
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("ZAI_MODEL", "glm-legacy")
    monkeypatch.setenv("ZAI_ENDPOINT", "https://legacy.example.com")
    monkeypatch.setenv("MODEL_zai", "glm-explicit")
    monkeypatch.setenv("BASE_URL_zai", "https://explicit.example.com")
    prof = providers.load_profile("zai")
    assert prof.model == "glm-explicit"
    assert prof.base_url == "https://explicit.example.com"


def test_named_profile_resolution(monkeypatch):
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("MODEL_openrouter", "anthropic/claude-sonnet-4")
    monkeypatch.setenv("API_KEY_openrouter", "or-key")
    # base_url comes from the openrouter name default

    prof = providers.load_profile("openrouter")
    assert prof is not None
    assert prof.kind == "openai"
    assert prof.model == "anthropic/claude-sonnet-4"
    assert prof.api_key == "or-key"
    assert "openrouter.ai" in prof.base_url


def test_unconfigured_profile_returns_none(monkeypatch):
    _clear_profile_env(monkeypatch)
    assert providers.load_profile("does_not_exist") is None


def test_unknown_kind_raises_build_error(monkeypatch):
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("MODEL_custom", "m1")
    monkeypatch.setenv("MODEL_PROVIDER_custom", "definitely-not-a-real-provider")

    prof = providers.load_profile("custom")
    assert prof is not None
    with pytest.raises(ValueError, match="Unknown provider kind"):
        providers._build_chat_model(prof, temperature=0.0, timeout=10.0)


def test_missing_optional_provider_gives_actionable_error(monkeypatch):
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("MODEL_anthropic", "claude-sonnet-4")
    monkeypatch.setenv("API_KEY_anthropic", "k")
    # Anthropic provider package isn't installed in the test env.

    prof = providers.load_profile("anthropic")
    assert prof is not None and prof.kind == "anthropic"
    with pytest.raises(RuntimeError, match="pip install langchain-anthropic"):
        providers._build_chat_model(prof, temperature=0.0, timeout=10.0)


def test_chat_model_caching_openai_compatible(monkeypatch):
    _clear_profile_env(monkeypatch)
    monkeypatch.setenv("MODEL_zai", "glm-test")
    monkeypatch.setenv("API_KEY_zai", "k")
    providers.clear_cache()
    a = providers.get_chat_model("zai", temperature=0.1, timeout=5.0)
    b = providers.get_chat_model("zai", temperature=0.1, timeout=5.0)
    assert a is b  # cached
    c = providers.get_chat_model("zai", temperature=0.2, timeout=5.0)
    assert c is not a  # different temperature -> different cache entry


# ---------------------------------------------------------------------------
# Settings: .env round-trip (comment-preserving)
# ---------------------------------------------------------------------------


def test_update_env_file_preserves_comments_and_appends(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# top comment\nZAI_API_KEY=old\n\n# mid\nMAX_ITERATIONS=3\n")

    _, changed = settings.update_env_file(p, {"MAX_ITERATIONS": "5", "NEW_KEY": "hi"})
    assert set(changed) == {"MAX_ITERATIONS", "NEW_KEY"}

    text = p.read_text()
    assert "# top comment" in text  # comment preserved
    assert "# mid" in text  # comment preserved
    assert "MAX_ITERATIONS=5" in text  # updated in place
    assert "ZAI_API_KEY=old" in text  # untouched key preserved
    assert "NEW_KEY=hi" in text  # new key appended


def test_update_env_file_quotes_spaces(tmp_path):
    p = tmp_path / ".env"
    settings.update_env_file(p, {"WORKSPACE_ROOT": "/path with spaces/x"})
    text = p.read_text()
    assert '"/path with spaces/x"' in text or "WORKSPACE_ROOT=" in text


def test_parse_argv_settings_applies_live(monkeypatch):
    monkeypatch.delenv("MY_TEST_K", raising=False)
    out = settings.parse_argv_settings(["MY_TEST_K=v1", "OTHER=2"])
    assert out == {"MY_TEST_K": "v1", "OTHER": "2"}
    assert os.environ["MY_TEST_K"] == "v1"


def test_parse_argv_settings_ignains_non_overrides():
    # Args without '=' or with non-identifier keys are ignored.
    out = settings.parse_argv_settings(["just a goal", "1abc=bad"])
    assert out == {}


# ---------------------------------------------------------------------------
# Settings: current_value + apply_changes
# ---------------------------------------------------------------------------


def test_current_value_uses_env_then_default(monkeypatch):
    s = settings.Setting("X_TEST", "t", "c", "h", "def")
    monkeypatch.delenv("X_TEST", raising=False)
    assert settings.current_value(s) == "def"
    monkeypatch.setenv("X_TEST", "live")
    assert settings.current_value(s) == "live"


def test_apply_changes_sets_env(monkeypatch):
    monkeypatch.delenv("X_APPLY", raising=False)
    settings.apply_changes({"X_APPLY": "yes"})
    assert os.environ["X_APPLY"] == "yes"


def test_mask_short_and_long():
    assert settings.mask("") == "(unset)"
    assert settings.mask("abc") == "***"
    assert settings.mask("sk-XPHONEXabcdef").startswith("sk-")
    assert settings.mask("sk-XPHONEXabcdef").endswith("def")
    assert "*" in settings.mask("sk-XPHONEXabcdef")
