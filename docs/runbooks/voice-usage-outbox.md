# Voice usage outbox recovery

Phone, Google Meet, and owner-voice lifecycle callbacks write one immutable
`VOICEUSAGE#<transaction>` event together with one `WORK#VOICEUSAGE#<transaction>`
pointer in the messaging table. The existing DynamoDB Stream → EventBridge Pipe →
`openclaw-work.fifo` path delivers the pointer. The worker sends the exact event
to Metronome with its original transaction id, records the accepted response,
and starts the frozen-period allowance check. A worker failure leaves the FIFO
message unacknowledged for retry.

The queue is bounded: 7-day main-queue retention, 30 receives, then the existing
14-day DLQ. A voice usage pointer in the DLQ is a billing recovery item and must
be redriven; never drop it. Listing is read-only:

```bash
cd apps/backend
uv run python -m scripts.openclaw_work_redrive --env dev --list
```

After reviewing the pointer identity and coordinating the environment-specific
recovery, redrive it with the existing tool:

```bash
uv run python -m scripts.openclaw_work_redrive --env dev --redrive --apply
```

The replay calls Metronome with the same transaction id (its ingest endpoint
deduplicates retries) and restarts the deterministic Temporal workflow if the
prior start did not land. The voice notice receipt prevents duplicate emails;
an SES result recorded as `unknown` is terminal and is never sent again.

Watch `messaging.worker.message` for `kind=voice_usage` retry/ok outcomes and
the existing OpenClaw work Pipe, queue, and DLQ alarms. `voice.usage_delivery`
records successful Metronome acceptance and Temporal scheduling steps. A
provider acceptance followed by a queue redrive is safe: the source receipt
records acceptance, so the worker skips duplicate provider writes and resumes
at workflow scheduling.

These mechanisms make event delivery durable through normal retries and
operator redrive, with the queue/DLQ retention above as the explicit recovery
boundary. They do not constitute an unlimited retention guarantee.
