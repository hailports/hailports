"""Team-aware triage router — assigns work items to team members.

Uses the team roster (data/team_roster.json) with focus tags and descriptions
to intelligently route work items to the right person.

Reasoning: Qwen3 local ($0) → Haiku fallback (~$0.001)
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent.parent
ROSTER_PATH = BASE_DIR / "data" / "team_roster.json"

FOCUS_LABELS = {
    "permissions_access": "Permissions & Access Management",
    "data_migration": "Data Migration & Imports",
    "flow_automation": "Flows & Automation",
    "reports_dashboards": "Reports & Dashboards",
    "page_layouts_ui": "Page Layouts & UI",
    "integration_api": "Integrations & API",
    "testing_qa": "Testing & QA",
    "documentation": "Documentation & SOPs",
    "apex_development": "Apex Development",
    "admin_config": "Admin Configuration",
    "user_support": "User Support & Training",
    "deployment": "Deployment & Release Management",
}


def load_roster():
    """Load team roster from JSON."""
    if not ROSTER_PATH.exists():
        return {"members": [], "groups": {}, "focus_tags": []}
    try:
        return json.loads(ROSTER_PATH.read_text())
    except Exception as e:
        log.warning(f"Failed to load roster: {e}")
        return {"members": [], "groups": {}, "focus_tags": []}


def save_roster(data):
    """Save team roster to JSON."""
    from datetime import datetime, timezone, timedelta
    data["updated"] = datetime.now(timezone(timedelta(hours=-6))).isoformat(timespec="seconds")
    ROSTER_PATH.write_text(json.dumps(data, indent=2))


def get_active_members():
    """Get all active team members."""
    roster = load_roster()
    return [m for m in roster.get("members", []) if m.get("active", True)]


def get_member(member_id):
    """Get a specific team member by ID."""
    roster = load_roster()
    for m in roster.get("members", []):
        if m["id"] == member_id:
            return m
    return None


def update_member(member_id, updates):
    """Update a team member's fields."""
    roster = load_roster()
    for m in roster.get("members", []):
        if m["id"] == member_id:
            m.update(updates)
            save_roster(roster)
            return m
    return None


def add_member(name, email, group="offshore_dev", focus=None, description=""):
    """Add a new team member."""
    roster = load_roster()
    member_id = name.lower().replace(" ", "_").replace(",", "")
    member = {
        "id": member_id,
        "name": name,
        "email": email,
        "group": group,
        "focus": focus or ["admin_config"],
        "description": description,
        "active": True,
    }
    roster.setdefault("members", []).append(member)
    save_roster(roster)
    return member


def remove_member(member_id):
    """Deactivate a team member."""
    return update_member(member_id, {"active": False})


async def route_work_item(item_summary, item_category=None, item_object=None):
    """Route a work item to the best team member using local LLM reasoning.

    Returns: {"member": {...}, "reasoning": "...", "confidence": "high/medium/low", "source": "local/haiku"}
    """
    members = get_active_members()
    if not members:
        return {"member": None, "reasoning": "No active team members", "confidence": "low", "source": "none"}

    # Build team context for the LLM
    team_lines = []
    for m in members:
        focus_str = ", ".join([FOCUS_LABELS.get(f, f) for f in m.get("focus", [])])
        desc = m.get("description", "").strip()
        roster = load_roster()
        group_label = roster.get("groups", {}).get(m.get("group", ""), {}).get("label", m.get("group", ""))
        line = f"- {m['name']} ({group_label}): Focus: {focus_str}"
        if desc:
            line += f". Notes: {desc}"
        team_lines.append(line)

    team_context = "\n".join(team_lines)

    prompt = f"""You are a Salesforce team lead routing work items to team members.

TEAM:
{team_context}

WORK ITEM:
Summary: {item_summary}
{f'Category: {item_category}' if item_category else ''}
{f'Object: {item_object}' if item_object else ''}

Pick the BEST team member for this work item. Consider their focus areas and any notes about their strengths.

Reply in exactly this JSON format, nothing else:
{{"member_id": "the_id", "reasoning": "one sentence why", "confidence": "high or medium or low"}}"""

    # Try local first ($0)
    try:
        from core.local_client import chat as local_chat
        result = await local_chat(
            [{"role": "user", "content": prompt}],
            system="You are a team routing assistant. Reply ONLY with the JSON object requested.",
            max_tokens=2048,
        )
        if result and "{" in result:
            parsed = json.loads(result[result.index("{"):result.rindex("}") + 1])
            member = get_member(parsed.get("member_id", ""))
            if member:
                return {
                    "member": member,
                    "reasoning": parsed.get("reasoning", ""),
                    "confidence": parsed.get("confidence", "medium"),
                    "source": "local",
                }
    except Exception as e:
        log.warning(f"Local routing failed: {e}")

    # Haiku fallback (~$0.001)
    try:
        from core.api_client import APIClient
        from core import SETTINGS
        api = APIClient()
        r = await api.create_message(
            model=SETTINGS["routing"]["haiku_model"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
            system="You are a team routing assistant. Reply ONLY with the JSON object requested.",
        )
        for block in r.content:
            if block.type == "text" and "{" in block.text:
                parsed = json.loads(block.text[block.text.index("{"):block.text.rindex("}") + 1])
                member = get_member(parsed.get("member_id", ""))
                if member:
                    return {
                        "member": member,
                        "reasoning": parsed.get("reasoning", ""),
                        "confidence": parsed.get("confidence", "medium"),
                        "source": "haiku",
                    }
    except Exception as e:
        log.warning(f"Haiku routing failed: {e}")

    # Rule-based fallback — match by focus tags
    if item_category:
        for m in members:
            if item_category in m.get("focus", []):
                return {
                    "member": m,
                    "reasoning": f"Focus tag match: {item_category}",
                    "confidence": "low",
                    "source": "rule",
                }

    return {
        "member": members[0],
        "reasoning": "Default assignment — no strong match found",
        "confidence": "low",
        "source": "default",
    }
