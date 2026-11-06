from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.auth.dependencies import current_user
from app.core.runtime import preferences

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
