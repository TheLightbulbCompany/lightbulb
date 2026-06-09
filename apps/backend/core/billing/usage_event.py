"""Normalized usage event — the contract between the AWS meter and the ledger.

The meter (Plan 3) parses a Bedrock invocation-log record, prices it, and emits
one of these. The ledger (`LedgerBackend.deplete`) consumes it. Keeping this a
single frozen dataclass means the meter and ledger evolve against one explicit
shape, and a future Metronome adapter consumes the identical event.

Money is in microcents (int): 1 cent = 10_000 microcents. `billed_microcents`
is the marked-up amount to deduct; `raw_cost_microcents` + `markup_multiplier`
are carried for audit only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UsageEvent:
    owner_id: str
    """Billing owner (org_id for orgs, user_id for personal). AWS-attributed."""

    request_id: str
    """Bedrock invocation requestId — the exactly-once idempotency key."""

    model_id: str
    """The AIP ARN (or model id) the run invoked, from the log `modelId`."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int

    raw_cost_microcents: int
    """Un-marked-up cost priced from token counts. Audit only."""

    billed_microcents: int
    """Marked-up amount to deduct from the balance."""

    markup_multiplier: float
    """The markup applied (e.g. 1.4). Audit only."""

    region: str
    """Source region of the invocation (logs are per fan-out region)."""

    run_status: str
    """`end`, `error`, or `aborted` — we bill all three."""

    occurred_at: str
    """ISO8601 timestamp from the invocation log record."""

    source: str
    """Provenance, e.g. `bedrock_invocation_log`."""

    def __post_init__(self) -> None:
        if not self.owner_id:
            raise ValueError("owner_id must be non-empty")
        if not self.request_id:
            raise ValueError("request_id must be non-empty")
        if self.billed_microcents < 0:
            raise ValueError("billed_microcents must be >= 0")
