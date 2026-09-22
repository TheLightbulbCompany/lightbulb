"""AgentCore workspace memory: a Hermes MemoryProvider shared by every agent in one workspace.

Adapted from sfalsin/hermes-agentcore-memory (MIT License, Copyright (c) sfalsin):
https://github.com/sfalsin/hermes-agentcore-memory

Differences from the original, all deliberate:
- One namespace, ``/owners/<owner>/``, and the owner id as ``actorId``. The runtime role's
  policy allows reads only for that exact namespace and writes only for that actor, so a
  different value fails closed. ``namespacePath`` is never sent: the policy cannot bound a
  prefix walk.
- Writes go through ``CreateEvent`` (short-term events the memory's strategy extracts
  records from), not ``BatchCreateMemoryRecords``. Hermes already calls ``sync_turn`` off the
  turn path on its own serialized worker, so the write is inline there -- no second thread.
- Text is passed through upstream's ``redact_for_egress`` first: memory is shared by every
  agent in the workspace, so a key pasted in one DM must not resurface in another room.
- Recall is passive: no tools. Every failure degrades to "no recall", never to a failed turn.

Enabled by the bridge (``bridge.sync.normalize_profile_backends``) setting
``memory.provider: agentcore`` when the runtime carries ``AGENTCORE_MEMORY_ID``.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from agent.redact import redact_for_egress

logger = logging.getLogger(__name__)

# botocore service-model limits for bedrock-agentcore (boto3 1.42.89, the base image's).
_QUERY_MAX = 10_000     # SearchCriteria.searchQuery
_TEXT_MAX = 100_000     # Content.text
_SESSION_MAX = 100      # SessionId, pattern [a-zA-Z0-9][a-zA-Z0-9-_]*
_MAX_RESULTS = 5

# The bridge builds a fresh AIAgent (and so a fresh provider) every turn; the client is cached
# per process so recall doesn't pay client construction + TLS on each turn.
_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        import boto3
        from botocore.config import Config

        _CLIENT = boto3.client(
            "bedrock-agentcore", region_name=os.environ.get("AWS_REGION") or None,
            config=Config(connect_timeout=1, read_timeout=1.5, retries={"total_max_attempts": 1}))
    return _CLIENT


def _session_id(raw: str) -> str:
    """A Hermes session id squeezed into CreateEvent's SessionId pattern."""
    cleaned = re.sub(r"[^a-zA-Z0-9-_]+", "-", raw or "").lstrip("-_")[:_SESSION_MAX]
    return cleaned or "hermes"


class AgentCoreMemoryProvider(MemoryProvider):
    def __init__(self):
        self._memory_id = os.environ.get("AGENTCORE_MEMORY_ID", "").strip()
        self._owner = os.environ.get("ISOL8_OWNER_ID", "").strip()
        self._session = "hermes"

    @property
    def name(self) -> str:
        return "agentcore"

    def is_available(self) -> bool:
        return bool(self._memory_id and self._owner)

    def unavailable_reason(self) -> str:
        return "" if self.is_available() else "AGENTCORE_MEMORY_ID or ISOL8_OWNER_ID is not set."

    def initialize(self, session_id: str, **kwargs) -> None:
        # Subagents never load a provider in this Hermes (delegate passes skip_memory=True), so
        # every context that reaches here is a primary turn -- Paperclip routines included.
        self._session = _session_id(session_id)

    def system_prompt_block(self) -> str:
        return ("Recalled workspace memory may appear in your context; it is shared by every "
                "agent in this workspace and may come from any room.")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not (query and self.is_available()):
            return ""
        try:
            resp = _client().retrieve_memory_records(
                memoryId=self._memory_id, namespace=f"/owners/{self._owner}/",
                searchCriteria={"searchQuery": query[:_QUERY_MAX]}, maxResults=_MAX_RESULTS)
            texts = [(r.get("content") or {}).get("text", "") for r in resp.get("memoryRecordSummaries", [])]
            texts = [t.strip() for t in texts if t and t.strip()]
        except Exception as exc:  # recall is best-effort: a slow or denied call is "no memory"
            logger.warning("agentcore memory recall failed: %s", exc)
            return ""
        logger.info("agentcore memory recall: %d records", len(texts))
        return "\n".join(f"- {t}" for t in texts)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", **_) -> None:
        if not self.is_available():
            return
        payload = [{"conversational": {"content": {"text": redact_for_egress(text)[:_TEXT_MAX]}, "role": role}}
                   for text, role in ((user_content, "USER"), (assistant_content, "ASSISTANT"))
                   if text and text.strip()]
        if not payload:
            return
        session = _session_id(session_id) if session_id else self._session
        try:
            _client().create_event(
                memoryId=self._memory_id, actorId=self._owner, sessionId=session,
                eventTimestamp=datetime.now(timezone.utc), payload=payload)
        except Exception as exc:
            logger.warning("agentcore memory write failed: %s", exc)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []


def register(ctx) -> None:
    ctx.register_memory_provider(AgentCoreMemoryProvider())
