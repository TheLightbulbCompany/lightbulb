"""Routing table for the platform Slack app: one row per installed workspace.

Slack posts every workspace's events to ONE Request URL with ``team_id`` in the
body, so ``team_id`` is the partition key; a ``by-owner`` GSI lists an owner's
workspaces. The bot token is NOT stored here — ``token_secret_name`` points at
the Secrets Manager entry (``integration_secret_store``) that holds it.

``status`` is ``"active"`` or ``"revoked"`` (after ``app_uninstalled`` /
``tokens_revoked``). A revoked row keeps its ``team_id`` so a later reinstall —
by the same or a different owner — can claim it; an ACTIVE row can only be
overwritten by its own owner (no workspace hijack).

Prefix-convention table (``core.dynamodb.get_table``), matching
``agent_email_repo``.
"""

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"


def _get_table():
    return get_table("slack-installs")


async def put(
    *,
    team_id: str,
    owner_id: str,
    agent_id: str,
    team_name: str,
    bot_user_id: str,
    app_id: str,
    scopes: list[str],
    installer_slack_user_id: str,
    installer_member_id: str,
    token_secret_name: str,
) -> dict | None:
    """Claim ``team_id`` for ``owner_id``. Conditional put:
    ``attribute_not_exists(team_id) OR owner_id = :owner OR #st = :revoked``.
    Returns the written item, or ``None`` when another owner holds an ACTIVE
    install for this workspace."""
    now = utc_now_iso()
    item = {
        "team_id": team_id,
        "owner_id": owner_id,
        "agent_id": agent_id,
        "team_name": team_name,
        "bot_user_id": bot_user_id,
        "app_id": app_id,
        "scopes": list(scopes),
        "installer_slack_user_id": installer_slack_user_id,
        "installer_member_id": installer_member_id,
        "token_secret_name": token_secret_name,
        "status": STATUS_ACTIVE,
        "installed_at": now,
        "updated_at": now,
    }
    try:
        await run_in_thread(
            _get_table().put_item,
            Item=item,
            ConditionExpression="attribute_not_exists(team_id) OR owner_id = :owner OR #st = :revoked",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":owner": owner_id, ":revoked": STATUS_REVOKED},
        )
        return item
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise


async def get(team_id: str) -> dict | None:
    """Strongly consistent — the events path decides routing on it."""
    resp = await run_in_thread(_get_table().get_item, Key={"team_id": team_id}, ConsistentRead=True)
    return resp.get("Item")


async def list_for_owner(owner_id: str) -> list[dict]:
    resp = await run_in_thread(
        _get_table().query,
        IndexName="by-owner",
        KeyConditionExpression=Key("owner_id").eq(owner_id),
    )
    return resp.get("Items", [])


async def set_status(team_id: str, status: str, *, expected_owner_id: str | None = None) -> bool:
    """No-op for an unknown team — never upserts a partial row. With ``expected_owner_id``, only
    an ACTIVE row of that owner changes (a revoke must not reach a workspace another owner has
    installed since). False when nothing was written."""
    condition, values = "attribute_exists(team_id)", {":s": status, ":t": utc_now_iso()}
    if expected_owner_id is not None:
        condition = "owner_id = :o AND #st = :active"
        values.update({":o": expected_owner_id, ":active": STATUS_ACTIVE})
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"team_id": team_id},
            UpdateExpression="SET #st = :s, updated_at = :t",
            ConditionExpression=condition,
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues=values,
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


async def delete_all_for_owner(owner_id: str) -> list[str]:
    """Teardown: delete every install row for ``owner_id``. Returns team ids."""
    rows = await list_for_owner(owner_id)
    table = _get_table()
    deleted: list[str] = []
    for row in rows:
        team_id = row.get("team_id")
        if not team_id:
            continue
        await run_in_thread(table.delete_item, Key={"team_id": team_id})
        deleted.append(team_id)
    return deleted
