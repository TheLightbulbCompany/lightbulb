"""A person signed in with a real Clerk session token (RS256, checked by get_current_user against a test JWKS)."""

import time
from dataclasses import dataclass

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from core import auth
from core.config import settings

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = {**jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True), "kid": "test", "use": "sig"}


@dataclass(frozen=True)
class Tenant:
    user_id: str
    claim: str | None  # an `o.id` the token carries (ignored by the backend)
    role: str | None = "admin"  # the `o.rol` beside it, or none
    token: str = "session"  # or "oauth" (an `at+jwt` access token), "template" (a JWT-template token: no `sid`)

    @property
    def headers(self) -> dict:
        now = int(time.time())
        claims = {"sub": self.user_id, "iss": settings.CLERK_ISSUER, "iat": now, "exp": now + 3600}
        claims |= {"email": f"{self.user_id}@example.com"}
        if self.token != "template":
            claims["sid"] = f"sess_{self.user_id}"
        if self.token == "oauth":
            claims["client_id"] = "oauth_app"
        if self.claim:
            claims["o"] = {"id": self.claim, "slg": self.claim, **({"rol": self.role} if self.role else {})}
        typ = {"typ": "at+jwt"} if self.token == "oauth" else {}
        token = jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": "test", **typ})
        return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def jwks(monkeypatch) -> None:
    """get_current_user checks the tokens Tenant mints against this key."""

    async def keys(url):
        return {"keys": [_JWK]}

    monkeypatch.setattr(auth, "_get_cached_jwks", keys)
