"""What AgentCore does with a metadata filter (long-term-memory-metadata.html, read 28 Oct 2026):
the system fields take ``dateTimeValue`` with BEFORE/AFTER only; a bare ``createdAt``/``updatedAt``
is not an indexed key and the service answers ValidationException."""

from botocore.exceptions import ClientError

SYSTEM_KEYS = {"x-amz-agentcore-memory-createdAt", "x-amz-agentcore-memory-updatedAt"}


def check_filters(filters: list[dict], op: str) -> None:
    for f in filters or []:
        key, value = f["left"]["metadataKey"], (f.get("right") or {}).get("metadataValue") or {}
        system = key in SYSTEM_KEYS
        bad = (
            key in {"createdAt", "updatedAt"}
            or (system and (f["operator"] not in {"BEFORE", "AFTER"} or set(value) != {"dateTimeValue"}))
            or (not system and "dateTimeValue" in value)
        )
        if bad:
            raise ClientError({"Error": {"Code": "ValidationException", "Message": f"bad filter {f}"}}, op)
