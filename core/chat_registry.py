"""Shared conversational tool registry for WebUI, Telegram, and iMessage."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from core.tool_registry import ToolRegistry


TokenGetter = Callable[[str], Awaitable[object]]


def _register_optional(registry: ToolRegistry, factory: Callable[[], object]) -> None:
    try:
        tool = factory()
    except Exception:
        return
    try:
        registry.register(tool)
    except Exception:
        return


def build_chat_tool_registry(get_token: TokenGetter) -> ToolRegistry:
    """Build the high-power chat registry shared by all conversational frontends."""
    profile = str(os.environ.get("CLAUDE_STACK_CHAT_PROFILE") or "").strip().lower()

    if profile in {"local_hands", "hands", "local"}:
        from tools.hands_tool import HandsTool

        registry = ToolRegistry()
        registry.register(HandsTool())
        return registry

    from tools.claude_code import ClaudeCodeTool
    from tools.apple_contacts import AppleContactsTool
    from tools.apple_notes import AppleNotesTool
    from tools.apple_reminders import AppleRemindersTool
    from tools.apple_shortcuts import AppleShortcutsTool
    from tools.code_runner import CodeRunnerTool
    from tools.document_output import DocumentOutputTool
    from tools.fantastical_local import FantasticalLocalTool
    from tools.google_drive import GoogleDriveTool
    from tools.hands_tool import HandsTool
    from tools.littlebird_local import LittlebirdLocalTool
    from tools.monday import MondayTool
    from tools.outlook_local import OutlookLocalTool
    from tools.sale_readiness import SaleReadinessTool
    from tools.sharepoint_local import SharePointLocalTool
    from tools.slack import SlackTool
    from tools.salesforce_ticket_read import SalesforceTicketReadTool
    from tools.salesforce_ticket_write import SalesforceTicketWriteTool
    from tools.strategy_ops import StrategyOpsTool
    from tools.image_production import ImageProductionTool
    from tools.web_search import WebSearchTool
    from tools.zoom_local import ZoomLocalTool

    registry = ToolRegistry()
    registry.register(SaleReadinessTool())
    registry.register(MondayTool())
    registry.register(OutlookLocalTool())
    registry.register(ZoomLocalTool())
    registry.register(GoogleDriveTool(get_token))
    registry.register(SharePointLocalTool())
    registry.register(FantasticalLocalTool())
    registry.register(LittlebirdLocalTool())
    registry.register(ImageProductionTool())
    registry.register(StrategyOpsTool())
    registry.register(DocumentOutputTool())
    registry.register(AppleContactsTool())
    registry.register(AppleNotesTool())
    registry.register(AppleRemindersTool())
    registry.register(AppleShortcutsTool())
    registry.register(SlackTool(get_token))
    registry.register(SalesforceTicketReadTool(get_token))
    registry.register(SalesforceTicketWriteTool(get_token))
    registry.register(WebSearchTool())
    registry.register(CodeRunnerTool())
    registry.register(ClaudeCodeTool())
    registry.register(HandsTool())

    _register_optional(registry, lambda: __import__("tools.sfdc_build_agent", fromlist=["SFDCBuildAgentTool"]).SFDCBuildAgentTool())
    _register_optional(registry, lambda: __import__("tools.sf_metadata", fromlist=["SFMetadataTool"]).SFMetadataTool())
    _register_optional(registry, lambda: __import__("tools.travel", fromlist=["TravelTool"]).TravelTool())
    _register_optional(registry, lambda: __import__("tools.revenue_funnel", fromlist=["RevenueFunnelTool"]).RevenueFunnelTool())
    _register_optional(registry, lambda: __import__("tools.reply_visibility", fromlist=["ReplyVisibilityTool"]).ReplyVisibilityTool())
    _register_optional(registry, lambda: __import__("tools.local_llm", fromlist=["LocalLLMTool"]).LocalLLMTool())
    _register_optional(registry, lambda: __import__("tools.redacted_memory", fromlist=["redactedMemoryTool"]).redactedMemoryTool())
    _register_optional(registry, lambda: __import__("tools.redacted_claude_bridge", fromlist=["redactedClaudeBridgeTool"]).redactedClaudeBridgeTool())
    _register_optional(registry, lambda: __import__("tools.persona_mail", fromlist=["PersonaMailTool"]).PersonaMailTool())
    _register_optional(registry, lambda: __import__("tools.databricks_query", fromlist=["DatabricksQueryTool"]).DatabricksQueryTool())
    _register_optional(registry, lambda: __import__("tools.github_read", fromlist=["GithubReadTool"]).GithubReadTool())
    _register_optional(registry, lambda: __import__("tools.github_reply", fromlist=["GithubReplyTool"]).GithubReplyTool())
    _register_optional(registry, lambda: __import__("tools.timecard", fromlist=["TimecardTool"]).TimecardTool())
    # Full Salesforce read/admin surface — ~55 tools already blast-radius-vetted in DIRECT_SAFE_TOOLS +
    # gate-classified in core/constants.py (writes fail-closed-gate on registration). Consolidates the
    # retired 8077 SF-admin GPT into StackGPT: org health/audits, access forensics ("why can't X see Y +
    # safe fix"), Apex/Flow source reads, user provisioning, password reset, permset/profile/role.
    _register_optional(registry, lambda: __import__("tools.salesforce", fromlist=["SalesforceTool"]).SalesforceTool(get_token))
    _register_optional(registry, lambda: __import__("tools.sf_power_tools", fromlist=["SFPowerTools"]).SFPowerTools(get_token))
    _register_optional(registry, lambda: __import__("tools.sf_admin_ops", fromlist=["SFAdminOpsTool"]).SFAdminOpsTool(get_token))
    return registry
