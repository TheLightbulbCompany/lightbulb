"""Pydantic response schemas for user endpoints."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class SyncUserRequest(BaseModel):
    """Optional legacy payload for POST /users/sync; extra fields are ignored."""


class SyncUserResponse(BaseModel):
    """Response from POST /users/sync."""

    status: str = Field(..., description="'created' or 'exists'")
    user_id: str = Field(..., description="Clerk user ID")


class UserPublicKeyResponse(BaseModel):
    """Response from GET /users/{user_id}/public-key."""

    user_id: str = Field(..., description="Clerk user ID")
    public_key: str = Field(..., description="X25519 public key (32 bytes hex)")


class CreateKeysResponse(BaseModel):
    """Response from POST /users/me/keys."""

    status: str = Field(..., description="'created'")
    public_key: str = Field(..., description="X25519 public key (32 bytes hex)")


class DeviceRegisterRequest(BaseModel):
    """Body for POST /api/v1/devices — one device's Expo push token."""

    token: str = Field(..., description="Expo push token, ExponentPushToken[...]")
    platform: Literal["ios", "android"] = Field(default="ios", description="Device platform")
    deviceId: str = Field(..., min_length=1, description="Stable per-install device id")
    timezone: str | None = Field(
        default=None,
        max_length=64,
        description="The device's IANA timezone (e.g. Europe/Paris). Stored on the user; an unknown zone is ignored.",
    )

    @field_validator("token")
    @classmethod
    def _looks_like_expo_token(cls, v: str) -> str:
        if not (v.startswith("ExponentPushToken[") or v.startswith("ExpoPushToken[")):
            raise ValueError("not an Expo push token")
        return v


class DeviceRegisterResponse(BaseModel):
    status: str = Field(..., description="'registered'")


NotifyLevel = Literal["off", "badge", "banner"]


class SessionNotifications(BaseModel):
    """Session notifications per kind: off / badge (Dock count and dot, no banner) / banner."""

    waiting: NotifyLevel = Field(default="banner", description="A permission prompt or a question")
    finished: NotifyLevel = Field(default="banner", description="A turn of a minute or more ended")
    attention: NotifyLevel = Field(default="badge", description="A job left a hand-off note")
    onlyUnfocused: bool = Field(default=False, description="No banner while Lightbulb is the focused app")
    sound: bool = True


class UserPrefs(BaseModel):
    """GET/PUT /api/v1/users/me/prefs response."""

    timezone: str = Field(..., description="IANA timezone; America/New_York when never set")
    dailySummary: bool = Field(..., description="Daily 'needs you' digest (push + email); default on")
    awayEmail: bool = Field(
        default=True, description="Email a DM, @mention or approval left unseen 5 minutes while away; default on"
    )
    chatTheme: Literal["light", "dark"] | None = Field(
        default=None,
        description="Account chat theme; null when never set on any device (client falls back to OS/localStorage)",
    )
    sessionsReachableFromBackgroundWork: bool = Field(
        default=True,
        description="Paperclip runs, heartbeats and routines may reach this person's shared sessions; default on",
    )
    sessionNotifications: SessionNotifications = Field(default_factory=SessionNotifications)


class UserPrefsUpdate(BaseModel):
    """PUT /api/v1/users/me/prefs body. Omitted fields are left unchanged."""

    timezone: str | None = Field(default=None, max_length=64, description="IANA timezone, e.g. Europe/Paris")
    dailySummary: bool | None = None
    awayEmail: bool | None = None
    chatTheme: Literal["light", "dark"] | None = None
    sessionsReachableFromBackgroundWork: bool | None = None
    #: The whole object; a client sends every field it shows.
    sessionNotifications: SessionNotifications | None = None

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, v: str | None) -> str | None:
        from core.repositories.user_repo import valid_timezone

        if v is not None and not valid_timezone(v):
            raise ValueError("unknown IANA timezone")
        return v
