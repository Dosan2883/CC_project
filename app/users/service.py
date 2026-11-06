"""UI·원문·번역·자막 언어를 분리하여 저장한다."""
from fastapi import HTTPException

from app.core.config import LANGUAGES, UI_LANGUAGES

DEFAULTS = {"uiLocale": "ko", "sourceLanguage": "ko", "defaultTargetLanguage": "ko", "preferredSubtitleLanguage": "ko"}


class PreferencesService:
    def __init__(self, db):
        self.db = db

    def get(self, user):
        record = self.db.get("USER#" + user.casefold(), "PREFERENCES")
        return {**DEFAULTS, **(record.value if record else {})}

    def update(self, user, changes):
        for key, value in changes.items():
            allowed = UI_LANGUAGES if key == "uiLocale" else LANGUAGES
            if key not in DEFAULTS or value not in allowed:
                raise HTTPException(422, "INVALID_PREFERENCE")
        return self.db.update("USER#" + user.casefold(), "PREFERENCES", lambda value: {**DEFAULTS, **(value or {}), **changes})
