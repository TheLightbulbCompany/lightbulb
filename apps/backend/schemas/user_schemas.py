"""Pydantic response schemas for user endpoints."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class SyncUserRequest(BaseModel):
    """Optional body for POST /users/sync.

    The canonical write path for ``provider_choice`` / ``byo_provider``
    is POST /billing/trial-checkout (persists synchronously to
    billing_accounts before creating the Stripe Checkout session). The
    fields are kept on this schema so old frontends keep parsing, but
    the server silently ignores them.
    """

    provider_choice: Literal["chatgpt_oauth", "byo_key", "bedrock_claude"] | None = Field(
        default=None,
        description="DEPRECATED: writes via /billing/trial-checkout. Silently ignored on /users/sync.",
    )
    byo_provider: Literal["openai", "anthropic"] | None = Field(
        default=None,
        description="DEPRECATED: writes via /billing/trial-checkout. Silently ignored on /users/sync.",
    )


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
    platform: Literal["ios"] = Field(default="ios", description="Device platform")
    deviceId: str = Field(..., min_length=1, description="Stable per-install device id")

    @field_validator("token")
    @classmethod
    def _looks_like_expo_token(cls, v: str) -> str:
        if not (v.startswith("ExponentPushToken[") or v.startswith("ExpoPushToken[")):
            raise ValueError("not an Expo push token")
        return v


class DeviceRegisterResponse(BaseModel):
    status: str = Field(..., description="'registered'")
