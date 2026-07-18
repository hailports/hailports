"""Canonical constants shared across the stack."""

import os

WRITE_TOOLS = {
    # Salesforce
    "salesforce_create_record", "salesforce_update_record", "salesforce_delete_record",
    "salesforce_create_chatter", "salesforce_ticket_create", "salesforce_ticket_update",
    "salesforce_ticket_attach",
    "sf_power_bulk_update_apply_preview", "sf_power_bulk_update_rollback",
    "sf_power_data_load_apply_preview", "sf_power_data_load_rollback",
    # Outlook
    "outlook_send_email", "outlook_create_draft", "outlook_reply_to_email",
    "outlook_forward_email", "outlook_reply_draft", "outlook_forward_draft",
    "outlook_create_calendar_event", "outlook_create_event",
    "outlook_create_contact", "outlook_delete_email", "outlook_flag_email",
    "outlook_mark_as_read", "outlook_mark_as_unread", "outlook_move_email",
    "outlook_accept_event", "outlook_decline_event", "outlook_tentative_event",
    # Monday
    "monday_create_item", "monday_update_item", "monday_add_update", "monday_attach_file",
    # Zoom
    "zoom_schedule_meeting", "zoom_join_meeting", "zoom_leave_meeting",
    "zoom_start_meeting", "zoom_send_direct_message", "zoom_send_meeting_chat",
    "zoom_toggle_mute", "zoom_toggle_video", "zoom_toggle_recording",
    "zoom_toggle_screenshare", "zoom_open_contacts",
    "zoom_reply_to_message", "zoom_react_to_message", "zoom_attach_file",
    # Slack
    "slack_send_message",
    # Calendar
    "calendar_create_event",
    # Documents
    "save_document",
    # Apple Mail
    "apple_mail_send",
    # Persona-owned outbound
    "docsapp_send_email", "maroon_standard_send_email",
    "image_production_configure",
    # Apple personal OS tools
    "apple_notes_create",
    "apple_reminders_create", "apple_reminders_complete",
    "apple_shortcuts_run",
}

COMMUNICATION_TOOLS = {
    "apple_mail_send",
    "outlook_send_email",
    "outlook_reply_to_email",
    "outlook_forward_email",
    "outlook_accept_event",
    "outlook_decline_event",
    "outlook_tentative_event",
    "outlook_create_calendar_event",
    "outlook_create_event",
    "salesforce_create_chatter",
    "sf_reset_user_password",
    "sf_reset_sandbox_user_password",
    "slack_send_message",
    "zoom_reply_to_message",
    "zoom_send_direct_message",
    "zoom_send_meeting_chat",
    "docsapp_send_email",
    "maroon_standard_send_email",
}

DEPLOY_TOOLS = {
    "sf_deploy_to_prod",
}

SALESFORCE_PROD_WRITE_TOOLS = {
    "salesforce_create_record",
    "salesforce_update_record",
    "salesforce_delete_record",
    "salesforce_create_chatter",
    "salesforce_ticket_create",
    "salesforce_ticket_update",
    "salesforce_ticket_attach",
    "salesforce_apply_access_fix",
    "salesforce_transfer_record_owner",
    "salesforce_create_record_share",
    "salesforce_remove_record_share",
    "salesforce_add_user_to_queue",
    "salesforce_remove_user_from_queue",
    "salesforce_add_user_to_owning_queue",
    "salesforce_remove_user_from_owning_queue",
    "salesforce_add_record_team_member",
    "salesforce_remove_record_team_member",
    "salesforce_revert_change",
    "salesforce_panic_revert_recent",
    "sf_reset_user_password",
    "sf_deploy_to_prod",
    "sf_power_bulk_update_apply_preview",
    "sf_power_bulk_update_rollback",
    "sf_power_data_load_apply_preview",
    "sf_power_data_load_rollback",
    # sf_* short-name admin write tools — MUST require explicit approval (no ungated prod writes).
    # Regressed 2026; restored under Phase 0 corp-write gating. Keep in sync with the `must` set in
    # tools/invariants_guard.py::chk_corp_writes_gated.
    "sf_assign_permission_set",
    "sf_remove_permission_set",
    "sf_apply_access_fix",
    "sf_create_field",
    "sf_update_picklist_deps",
    "sf_create_validation_rule",
    "sf_create_sharing_rule",
    "sf_merge_records",
    "sf_move_contact",
    "sf_transfer_record_owner",
    "sf_deploy_flow",
    "sf_restore_metadata_change",
    "sf_bulk_update",
    "sf_create_record",
    "sf_update_record",
}

DESTRUCTIVE_TOOLS = {
    "salesforce_delete_record",
    "sf_power_bulk_update_apply_preview",
    "sf_power_bulk_update_rollback",
    "sf_power_data_load_apply_preview",
    "sf_power_data_load_rollback",
    "outlook_delete_email",
}

WEBUI_DRAFT_EQUIVALENTS = {
    "outlook_send_email": "outlook_create_draft",
    "outlook_reply_to_email": "outlook_reply_draft",
    "outlook_forward_email": "outlook_forward_draft",
}

WEBUI_DRAFT_CONFIRMATION_TOOLS = {
    "outlook_create_draft",
    "outlook_reply_draft",
    "outlook_forward_draft",
}

WEBUI_CONFIRMATION_ONLY_TOOLS = WEBUI_DRAFT_CONFIRMATION_TOOLS | {
    "hands_confirm_action",
}

APPROVAL_REQUIRED_TOOLS = COMMUNICATION_TOOLS | DESTRUCTIVE_TOOLS | SALESFORCE_PROD_WRITE_TOOLS | {
    "hands_confirm_action",
    "apple_notes_create",
    "apple_reminders_create",
    "apple_reminders_complete",
    "apple_shortcuts_run",
    "image_production_configure",
}


COST_RATES = {
    "haiku":  {"input": 0.25,  "output": 1.25},
    "sonnet": {"input": 3.0,   "output": 15.0},
    "opus":   {"input": 15.0,  "output": 75.0},
}

MODEL_COST_RATES = {
    "claude-haiku-4-5-20251001": COST_RATES["haiku"],
    "claude-sonnet-4-20250514":  COST_RATES["sonnet"],
    "claude-opus-4-20250514":    COST_RATES["opus"],
}

DEFAULT_LOCAL_MODEL = "qwen2.5:7b"
HOT_LOCAL_MODEL = os.environ.get("CLAUDE_STACK_LOCAL_MODEL", os.environ.get("LOCAL_MODEL", DEFAULT_LOCAL_MODEL))
HOT_MODEL_MUST_STAY_RESIDENT = True

# The Mini is currently optimized around one resident local model. Keep this
# environment-driven so memory purge work can remove large side models without
# breaking the chat/router stack.
LOCAL_MODEL = HOT_LOCAL_MODEL
FAST_LOCAL_MODEL = LOCAL_MODEL

OLLAMA_URL = "http://localhost:11434"
