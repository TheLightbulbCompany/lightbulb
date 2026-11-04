"""The A2A authority object: who asked, through which agents, for whom, which operation and operands.

Minted and verified as a compact EdDSA JWS signed with the network-proof issuer key.
"""

import hashlib
import json
import time
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from core.services import network_proof
from core.services.messaging import ContractModel
from core.services.network_proof import _sign, _unb64

URI = "https://thelightbulb.company/a2a/ext/authority/v1"
OBJECT_TYP = "lightbulb-a2a-authority"
RECORD_TYP = "lightbulb-a2a-record"
MAX_LIFE_S = 86_400
MAX_INTENT = 2048


class Operation(ContractModel):
    skill: str
    resource: str
    operands: dict


class AuthorityRequest(ContractModel):
    operation: Operation
    intent: str
    on_behalf_of: str | None = None


class AuthorityError(ValueError):
    """``check`` names the first failing check: signature, typ, exp, iat, shape, intent_length,
    on_behalf_of, origin, operands (not encodable) or operands_hash."""

    def __init__(self, check: str):
        super().__init__(check)
        self.check = check


def operands_hash(operands: dict) -> str:
    # ponytail: JCS-equivalent for JSON without floats; swap in an RFC 8785 canonicalizer when floats appear
    try:
        canon = json.dumps(operands, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    except ValueError:  # a lone surrogate (UnicodeEncodeError) or a value JSON cannot hold
        raise AuthorityError("operands") from None
    return "sha256:" + hashlib.sha256(canon).hexdigest()


def mint(
    *,
    requester_person: str,
    org: str,
    verified_by: str,
    chain: list[dict],
    request: AuthorityRequest,
    now: int | None = None,
) -> tuple[str, dict]:
    if len(request.intent) > MAX_INTENT:
        raise AuthorityError("intent_length")
    iat = int(time.time()) if now is None else now
    claims = {
        "v": 1,
        "id": str(uuid.uuid4()),
        "iat": iat,
        "exp": iat + MAX_LIFE_S,
        "requester": {"person": requester_person, "org": org, "verified_by": verified_by},
        "chain": chain,
        "operation": {
            "skill": request.operation.skill,
            "resource": request.operation.resource,
            "operands": request.operation.operands,
            "operands_hash": operands_hash(request.operation.operands),
        },
        "intent": request.intent,
        "on_behalf_of": request.on_behalf_of,
        "policy": None,
    }
    return _sign(claims, typ=OBJECT_TYP), claims


def sign_record(record: dict) -> str:
    return _sign(record, typ=RECORD_TYP)


def _is(value, kind) -> bool:
    return isinstance(value, kind) and not isinstance(value, bool)


def _shape_ok(c: dict) -> bool:
    req, op = c.get("requester"), c.get("operation")
    return (
        c.get("v") == 1
        and _is(c.get("id"), str)
        and isinstance(req, dict)
        and all(_is(req.get(k), str) and req[k] for k in ("person", "org", "verified_by"))
        and isinstance(c.get("chain"), list)
        and all(isinstance(h, dict) for h in c["chain"])
        and isinstance(op, dict)
        and all(_is(op.get(k), str) for k in ("skill", "resource", "operands_hash"))
        and isinstance(op.get("operands"), dict)
        and _is(c.get("intent"), str)
        and (c.get("on_behalf_of") is None or _is(c["on_behalf_of"], str))
        and c.get("policy") is None
    )


def verify(jws: str, *, now: int | None = None, typ: str = OBJECT_TYP) -> dict:
    """The claims when ``jws`` is our signed object (or record, with ``typ=RECORD_TYP``) in force at ``now``."""
    now = int(time.time()) if now is None else now
    try:
        h, p, s = jws.split(".")
        header = json.loads(_unb64(h))
        Ed25519PublicKey.from_public_bytes(_unb64(network_proof.public_key()["key"])).verify(
            _unb64(s), f"{h}.{p}".encode()
        )
        claims = json.loads(_unb64(p))
    except (ValueError, TypeError, AttributeError, InvalidSignature, RuntimeError):
        raise AuthorityError("signature") from None
    if header.get("alg") != "EdDSA" or header.get("kid") != network_proof.kid():
        raise AuthorityError("signature")
    if header.get("typ") != typ:
        raise AuthorityError("typ")
    if not isinstance(claims, dict) or not _is(claims.get("iat"), int):
        raise AuthorityError("shape")
    if claims["iat"] > now:
        raise AuthorityError("iat")
    exp = claims.get("exp")
    if typ == RECORD_TYP:
        if exp is not None and (not _is(exp, int) or now >= exp):
            raise AuthorityError("exp")
        return claims
    if not _is(exp, int) or now >= exp or exp - claims["iat"] > MAX_LIFE_S:
        raise AuthorityError("exp")
    if not _shape_ok(claims):
        raise AuthorityError("shape")
    if len(claims["intent"]) > MAX_INTENT:
        raise AuthorityError("intent_length")
    person = claims["requester"]["person"]
    if claims["on_behalf_of"] not in (None, person):
        raise AuthorityError("on_behalf_of")
    if not claims["chain"] or claims["chain"][0].get("person") != person:
        raise AuthorityError("origin")
    if claims["operation"]["operands_hash"] != operands_hash(claims["operation"]["operands"]):
        raise AuthorityError("operands_hash")
    return claims
