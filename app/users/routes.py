from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, StrictBool

from app.auth.dependencies import current_user
from app.core.runtime import ai_policy, preferences

router = APIRouter()


class PreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uiLocale: str | None = None
    sourceLanguage: str | None = None
    defaultTargetLanguage: str | None = None
    preferredSubtitleLanguage: str | None = None


@router.get("/api/users/me/preferences")
def get_preferences(user: str = Depends(current_user)):
    return preferences.get(user)


@router.patch("/api/users/me/preferences")
def update_preferences(body: PreferenceUpdate, user: str = Depends(current_user)):
    return preferences.update(user, body.model_dump(exclude_unset=True))


class AIConsentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed: StrictBool


@router.get("/api/users/me/ai-consent")
def get_ai_consent(user: str = Depends(current_user)):
    return ai_policy.consent(user)


@router.patch("/api/users/me/ai-consent")
def update_ai_consent(body: AIConsentUpdate, user: str = Depends(current_user)):
    return ai_policy.set_consent(user, body.allowed)
