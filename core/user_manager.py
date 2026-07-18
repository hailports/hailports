"""Maps frontend-specific IDs to internal user profiles."""

from core import USERS
from core.doc_branding import get_doc_preferences as _get_doc_preferences


class UserManager:
    def __init__(self):
        self._by_telegram = {}
        self._by_imessage = {}
        self._by_webui = {}
        self.profiles = {}

        for user_id, profile in USERS.items():
            self.profiles[user_id] = profile
            if profile.get("telegram_id"):
                self._by_telegram[profile["telegram_id"]] = user_id
            if profile.get("imessage_handle"):
                self._by_imessage[profile["imessage_handle"]] = user_id
            for h in profile.get("imessage_handles", []):
                self._by_imessage[h] = user_id
            if profile.get("webui_username"):
                self._by_webui[profile["webui_username"]] = user_id

    def resolve_telegram(self, chat_id):
        return self._by_telegram.get(chat_id)

    def resolve_imessage(self, handle):
        return self._by_imessage.get(handle)

    def resolve_webui(self, username):
        return self._by_webui.get(username)

    def get_profile(self, user_id):
        return self.profiles.get(user_id, {})

    def get_addon(self, user_id):
        return self.profiles.get(user_id, {}).get("system_prompt_addon", "")

    def is_owner(self, user_id):
        return self.profiles.get(user_id, {}).get("role", "") == "owner"

    def get_default_model(self, user_id):
        return self.profiles.get(user_id, {}).get("default_model", "sonnet")

    def is_known_telegram(self, chat_id):
        return chat_id in self._by_telegram

    def get_doc_preferences(self, user_id):
        return _get_doc_preferences(user_id)
