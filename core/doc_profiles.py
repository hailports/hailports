"""Document profile routing for high-coverage local-first generation."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

_DOC_REQUEST_RE = re.compile(
    r"(create|make|write|generate|draft|build|produce|design)\b.{0,50}\b"
    r"(doc|document|guide|report|process|playbook|runbook|template|presentation|brief|checklist|sop|procedure|manual|plan|proposal|qbr|postmortem|status report|decision record)",
    re.IGNORECASE,
)

_SF_RE = re.compile(
    r"\b(salesforce|sf|lightning|lead|opportunity|contact|account|case|ticket|sobject|apex|trigger|flow|"
    r"record type|permission set|profile|sharing rule|validation rule|picklist|field|object|"
    r"report|dashboard|chatter|sandbox|deploy|metadata|admin|crm|convert|lead conversion)\b",
    re.IGNORECASE,
)


def _contains(text: str, *patterns: str) -> bool:
    return any(re.search(p, text) for p in patterns)


_DOC_TYPES: Dict[str, Dict[str, Any]] = {
    "sf_end_user_howto": {
        "doc_type": "salesforce",
        "label": "Salesforce End-User How-To",
        "filename_hint": "Salesforce_User_Guide.html",
        "escalate_to_opus": False,
        "instructions": "Optimize for non-technical internal users. Keep the writing direct, visual, and confidence-building.",
        "section_blueprint": [
            "Overview and when to use this workflow",
            "Prerequisites and where to click first",
            "Step-by-step task walkthrough",
            "Verification: where the user confirms success",
            "Tips and best practices",
            "Troubleshooting common issues",
        ],
    },
    "sf_admin_runbook": {
        "doc_type": "salesforce",
        "label": "Salesforce Admin Runbook",
        "filename_hint": "Salesforce_Admin_Runbook.html",
        "escalate_to_opus": False,
        "instructions": "Write for admins and power users. Focus on exact configuration steps, validation, rollback, and deployment notes.",
        "section_blueprint": [
            "Objective and business outcome",
            "Scope, assumptions, and prerequisites",
            "Configuration steps in order",
            "Validation and sandbox test checklist",
            "Rollback or mitigation plan",
            "Deployment notes and follow-up",
        ],
    },
    "sf_training": {
        "doc_type": "salesforce",
        "label": "Salesforce Training Guide",
        "filename_hint": "Salesforce_Training_Guide.html",
        "escalate_to_opus": False,
        "instructions": "Teach the workflow clearly, explain why it matters, and close with a self-check or recap.",
        "section_blueprint": [
            "What this workflow does",
            "Key concepts and roles",
            "Guided walkthrough",
            "Common mistakes to avoid",
            "Practice or self-check",
            "Reference and support",
        ],
    },
    "corporate_sop": {
        "doc_type": "corporate",
        "label": "SOP / Procedure",
        "filename_hint": "Standard_Operating_Procedure.html",
        "escalate_to_opus": False,
        "instructions": "Use a precise operational tone. This should read like a repeatable procedure.",
        "section_blueprint": [
            "Purpose",
            "Scope and ownership",
            "Triggers and prerequisites",
            "Procedure steps",
            "Exceptions and escalation",
            "Verification checklist",
        ],
    },
    "corporate_runbook": {
        "doc_type": "corporate",
        "label": "Runbook / Playbook",
        "filename_hint": "Operational_Runbook.html",
        "escalate_to_opus": False,
        "instructions": "Prioritize actionability under time pressure. Make decisions and handoffs explicit.",
        "section_blueprint": [
            "Trigger conditions",
            "Immediate actions",
            "Decision points",
            "Escalation path",
            "Recovery or rollback",
            "Post-action verification",
        ],
    },
    "corporate_plan": {
        "doc_type": "corporate",
        "label": "Project / Implementation Plan",
        "filename_hint": "Implementation_Plan.html",
        "escalate_to_opus": False,
        "instructions": "Frame the work with clear milestones, risks, and owners.",
        "section_blueprint": [
            "Goal and current state",
            "Scope and constraints",
            "Workstreams and milestones",
            "Dependencies and risks",
            "Owner map",
            "Immediate next steps",
        ],
    },
    "corporate_brief": {
        "doc_type": "corporate",
        "label": "Executive / Decision Brief",
        "filename_hint": "Executive_Brief.html",
        "escalate_to_opus": False,
        "instructions": "Lead with recommendation, then context, tradeoffs, and the decision required.",
        "section_blueprint": [
            "Recommendation",
            "Context",
            "Options considered",
            "Tradeoffs and risks",
            "Decision requested",
        ],
    },
    "corporate_policy": {
        "doc_type": "corporate",
        "label": "Policy / Standard",
        "filename_hint": "Policy_Standard.html",
        "escalate_to_opus": False,
        "instructions": "Use clear policy language with controls, exceptions, and ownership.",
        "section_blueprint": [
            "Purpose",
            "Scope",
            "Policy statements",
            "Required controls",
            "Exceptions",
            "Owner and review cadence",
        ],
    },
    "corporate_training": {
        "doc_type": "corporate",
        "label": "Training / User Guide",
        "filename_hint": "Training_Guide.html",
        "escalate_to_opus": False,
        "instructions": "Write for learners. Sequence the content so a first-time reader can execute it without live help.",
        "section_blueprint": [
            "Outcome and context",
            "Before you begin",
            "Step-by-step walkthrough",
            "Examples",
            "Best practices",
            "Troubleshooting",
        ],
    },
    "status_report": {
        "doc_type": "corporate",
        "label": "Status Report",
        "filename_hint": "Status_Report.html",
        "escalate_to_opus": False,
        "instructions": "Summarize progress crisply with blockers, decisions, and asks.",
        "section_blueprint": [
            "Overall status snapshot",
            "Progress since last update",
            "Current blockers and risks",
            "Decisions needed",
            "Next 7-14 days",
        ],
    },
    "meeting_brief": {
        "doc_type": "corporate",
        "label": "Meeting Brief",
        "filename_hint": "Meeting_Brief.html",
        "escalate_to_opus": False,
        "instructions": "Prepare someone to walk into a meeting with context, objectives, and recommended talking points.",
        "section_blueprint": [
            "Meeting objective",
            "Who is involved",
            "Context and latest signals",
            "Recommended talking points",
            "Open questions",
            "Desired outcomes and follow-up",
        ],
    },
    "postmortem": {
        "doc_type": "corporate",
        "label": "Postmortem",
        "filename_hint": "Postmortem.html",
        "escalate_to_opus": False,
        "instructions": "Be factual and calm. Focus on timeline, impact, contributing factors, corrective actions, and owners.",
        "section_blueprint": [
            "Incident summary",
            "Impact",
            "Timeline",
            "Root causes and contributing factors",
            "Corrective actions",
            "Owners and follow-up dates",
        ],
    },
    "qbr": {
        "doc_type": "corporate",
        "label": "Quarterly Business Review",
        "filename_hint": "Quarterly_Business_Review.html",
        "escalate_to_opus": False,
        "instructions": "Make performance, wins, misses, and next-quarter focus easy to scan.",
        "section_blueprint": [
            "Quarter overview",
            "Key metrics and trendline",
            "Wins",
            "Gaps and misses",
            "Strategic implications",
            "Next-quarter priorities",
        ],
    },
    "implementation_decision_record": {
        "doc_type": "corporate",
        "label": "Implementation Decision Record",
        "filename_hint": "Implementation_Decision_Record.html",
        "escalate_to_opus": False,
        "instructions": "Capture one implementation decision with context, options, chosen direction, and consequences.",
        "section_blueprint": [
            "Decision statement",
            "Context",
            "Options considered",
            "Chosen approach",
            "Consequences",
            "Follow-up actions",
        ],
    },
    "training_quickref": {
        "doc_type": "corporate",
        "label": "Training Quick Reference",
        "filename_hint": "Training_Quick_Reference.html",
        "escalate_to_opus": False,
        "instructions": "Keep it compact. Optimize for people who already had training and need a fast refresher.",
        "section_blueprint": [
            "Purpose",
            "Fast-start steps",
            "Key reminders",
            "Common mistakes",
            "Where to get help",
        ],
    },
    "customer_facing_guide": {
        "doc_type": "corporate",
        "label": "Customer-Facing Guide",
        "filename_hint": "Customer_Facing_Guide.html",
        "escalate_to_opus": False,
        "instructions": "Use a polished external-facing tone. Keep wording clear, reassuring, and low-jargon.",
        "section_blueprint": [
            "Overview",
            "What the customer should expect",
            "Steps or workflow",
            "Helpful tips",
            "Support path",
        ],
    },
    "general_guide": {
        "doc_type": "corporate",
        "label": "General Guide",
        "filename_hint": "Document.html",
        "escalate_to_opus": False,
        "instructions": "Use a balanced structure with overview, core content, practical guidance, and wrap-up.",
        "section_blueprint": [
            "Overview",
            "Current context",
            "Core guidance",
            "Recommended actions",
            "Follow-up or reference",
        ],
    },
    "one_off_opus": {
        "doc_type": "corporate",
        "label": "One-Off Custom Document",
        "filename_hint": "Custom_Document.html",
        "escalate_to_opus": True,
        "instructions": "The request is unusually bespoke or high-variance, so route it to Opus with explicit structure.",
        "section_blueprint": [
            "Tailor the structure to the request",
        ],
    },
}


def detect_doc_profile(text: str) -> Optional[Dict[str, Any]]:
    if not _DOC_REQUEST_RE.search(text):
        return None

    lower = text.lower()
    is_sf = bool(_SF_RE.search(text))

    if is_sf:
        if _contains(lower, r"\b(chatter|upload|attachment|attach file|files related list|post file|composer)\b"):
            key = "sf_end_user_howto"
        elif _contains(lower, r"\b(training|enablement|learn|walkthrough|quick reference|how to use)\b"):
            key = "sf_training"
        else:
            key = "sf_admin_runbook"
    else:
        if _contains(lower, r"\b(one-off|one off|custom weird|totally custom|novel|board deck|press release|legal brief)\b"):
            key = "one_off_opus"
        elif _contains(lower, r"\b(postmortem|incident review|after action|root cause)\b"):
            key = "postmortem"
        elif _contains(lower, r"\b(qbr|quarterly business review|quarter review|business review)\b"):
            key = "qbr"
        elif _contains(lower, r"\b(status report|weekly update|monthly update|status update|project status)\b"):
            key = "status_report"
        elif _contains(lower, r"\b(meeting brief|prep brief|briefing|prep for meeting)\b"):
            key = "meeting_brief"
        elif _contains(lower, r"\b(adr|decision record|implementation decision|architecture decision)\b"):
            key = "implementation_decision_record"
        elif _contains(lower, r"\b(quick reference|cheat sheet|quick ref)\b"):
            key = "training_quickref"
        elif _contains(lower, r"\b(customer guide|client guide|client-facing|customer-facing|external guide)\b"):
            key = "customer_facing_guide"
        elif _contains(lower, r"\b(runbook|playbook|rollback|recovery|cutover)\b"):
            key = "corporate_runbook"
        elif _contains(lower, r"\b(sop|procedure|checklist|standard operating procedure|work instruction)\b"):
            key = "corporate_sop"
        elif _contains(lower, r"\b(project plan|implementation plan|roadmap|proposal|milestone)\b"):
            key = "corporate_plan"
        elif _contains(lower, r"\b(executive brief|decision brief|memo|summary for leadership)\b"):
            key = "corporate_brief"
        elif _contains(lower, r"\b(policy|standard|governance|control standard)\b"):
            key = "corporate_policy"
        elif _contains(lower, r"\b(training|enablement|user guide|onboarding|walkthrough|how to)\b"):
            key = "corporate_training"
        else:
            key = "general_guide"

    profile = dict(_DOC_TYPES[key])
    profile["id"] = key
    profile["is_doc_request"] = True
    profile["is_sf_doc"] = profile["doc_type"] == "salesforce"
    return profile


def build_doc_system_prompt(profile: Dict[str, Any], branding: Dict[str, Any], research_block: str = "") -> str:
    organization = branding.get("organization") or branding.get("display_name", "")
    author = branding.get("author") or branding.get("display_name", "")
    neutral = branding.get("brand_mode") == "neutral"
    include_cover = branding.get("include_cover", True)
    include_toc = branding.get("include_toc", True)
    filename_hint = profile.get("filename_hint", "Document.html")
    section_blueprint = "\n".join(f"- {section}" for section in profile.get("section_blueprint", []))
    cover_rule = "Start with div.cover" if include_cover else "Start with a concise h1 + subtitle block instead of a full cover"
    toc_rule = "Include div.toc after the cover" if include_toc else "Do not generate a table of contents block unless the content is long enough to justify it"

    base_rules = (
        "RULES:\n"
        "1. Write ONLY body HTML - no CSS, no <style>, no <html> wrapper\n"
        f"2. {cover_rule}, then {toc_rule}, then 5-8 numbered h2 sections unless the profile clearly needs fewer\n"
        f"3. Use this cover metadata exactly: Author ({author})" + (f", Organization ({organization})" if organization else "") + "\n"
        "4. Write concrete, professional content using real details from research when available\n"
        "5. Include div.caption below every diagram or visual\n"
        "6. ABSOLUTELY NO <img> tags. Build visuals using the allowed HTML/CSS classes only\n"
        "7. Do NOT reply with text - ONLY call the save_document tool\n"
        f"8. Call save_document with filename='{filename_hint}', doc_profile='{profile['id']}', doc_type='{profile['doc_type']}'\n"
        f"9. Use this section blueprint unless the request explicitly demands a different shape:\n{section_blueprint}\n"
    )

    if profile["doc_type"] == "salesforce":
        ui_rule = (
            "Use standard Salesforce Lightning chrome only. Do NOT use CompanyA, Valley, or company-specific logo bars."
            if neutral or branding.get("salesforce_branding") == "generic"
            else "Use the standard branded Salesforce Lightning shell for this environment."
        )
        persona = (
            "You are a precise document designer creating clean, professional Salesforce guidance."
            if neutral else
            f"You are a senior technical writer creating Salesforce guidance for {organization or 'the organization'}."
        )
        return (
            persona + " "
            "Call save_document with the document BODY HTML only.\n\n"
            f"PROFILE NAME: {profile['label']}\n"
            f"PROFILE INSTRUCTIONS: {profile['instructions']}\n"
            f"FILENAME CONVENTION: {filename_hint}\n"
            "AVAILABLE CSS CLASSES (use these exactly):\n"
            "- div.cover > h1 + div.subtitle + div.meta\n"
            "- div.toc > h2 + ol>li\n"
            "- div.callout > strong + text\n"
            "- div.flow > div.flow-step + div.flow-arrow\n"
            "- div.mockup with div.sf-browser-bar, div.sf-tabs, div.sf-body, div.sf-record-header, div.sf-highlights, div.sf-path, button.sf-btn\n"
            "- div.sf-dialog with div.sf-dialog-header, div.sf-dialog-body, div.sf-dialog-footer\n"
            "- table, div.result-tags, div.caption\n\n"
            "SALESFORCE-SPECIFIC RULES:\n"
            f"- {ui_rule}\n"
            "- Use div.sf-path ONLY for Lead and Opportunity record mockups\n"
            "- For Account, Contact, Case, and Chatter mockups, show layout, highlights, tabs, and composer without a sales path\n"
            "- Use realistic 18-character Salesforce IDs\n\n"
            + base_rules + research_block
        )

    persona = (
        "You are a sharp professional document designer creating clean, client-safe documents with no CompanyA-specific branding."
        if neutral else
        f"You are a senior technical writer preparing polished business documents for {organization or 'the organization'}."
    )
    corp_rule = (
        "Keep the visual system neutral, editorial, and broadly reusable. Never mention CompanyA unless the user explicitly asked for it."
        if neutral else
        f"Use a polished, branded business tone appropriate for {organization or 'the organization'}."
    )
    return (
        persona + " "
        "Call save_document with the document BODY HTML only.\n\n"
        f"PROFILE NAME: {profile['label']}\n"
        f"PROFILE INSTRUCTIONS: {profile['instructions']}\n"
        f"FILENAME CONVENTION: {filename_hint}\n"
        "AVAILABLE CSS CLASSES (use these exactly):\n"
        "- div.cover > h1 + div.subtitle + div.meta\n"
        "- div.toc > h2 + ol>li\n"
        "- div.callout > strong + text\n"
        "- div.flow > div.flow-step + div.flow-arrow\n"
        "- table > thead>tr>th + tbody>tr>td\n"
        "- div.caption\n"
        "- h2 for numbered sections, h3 for subsections, ul and ol for lists\n\n"
        "CORPORATE DOCUMENT RULES:\n"
        "- Do NOT use Salesforce mockup classes unless the request is explicitly about Salesforce UI\n"
        f"- {corp_rule}\n\n"
        + base_rules + research_block
    )
