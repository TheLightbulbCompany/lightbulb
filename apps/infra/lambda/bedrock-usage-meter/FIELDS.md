# Bedrock invocation-log record — field paths (captured dev, 2026-07-15)

Confirmed against real dev `ConverseStream` records (cache-heavy trial run,
2026-07-15): **all four token counts are TOP-LEVEL** — `input.inputTokenCount`,
`input.cacheReadInputTokenCount`, `input.cacheWriteInputTokenCount`,
`output.outputTokenCount` — and equal the response body's `usage` block
exactly. The body fallback paths in `index.py::_extract` are kept as
defensive-only.

## Bodies are unavoidable — text delivery MUST stay enabled

`textDataDeliveryEnabled` cannot be turned off: the modality flags select
WHICH MODELS get logged at all, not whether bodies are included — "Data will
be logged for all models that support the modalities ... that you choose"
(Bedrock user guide, model-invocation-logging). Deselecting Text stops
logging Claude/Converse invocations entirely, blinding the meter and killing
the trial cap. There is NO metadata-only mode.

Consequence: every record carries the full request/response bodies, and live
records were observed containing a tenant's Paperclip API key and gateway JWT
in plaintext. Treat `/lightbulb/bedrock-invocation` as a credential-bearing
surface. Mitigation is the log group's SHORT retention (3 days,
`bedrock-meter-stack.ts`) — the meter consumes records within seconds and DLQ
messages embed their own copy of the log event, so nothing re-reads the group
after delivery.

## No S3 — CloudWatch-only

The meter is **CloudWatch-only** and never touches S3. The S3-offload merge path
(and the synthetic S3 fixtures) were removed. Rationale:

- Token counts (`input.inputTokenCount`, `output.outputTokenCount`) are TOP-LEVEL
  inline in the CloudWatch record.
- The CACHE counts (`cacheReadInputTokens`/`cacheWriteInputTokens`, the Converse
  `TokenUsage`) live in the RESPONSE body at `output.outputBodyJson.usage`.
- Bedrock offloads only bodies **>100KB** to S3. The huge thing on cache-heavy
  turns is the cached INPUT prompt (`inputBodyJson`), which the meter does NOT
  need. The OUTPUT body (completion + `usage`) is almost always <100KB, so Bedrock
  inlines it in CloudWatch → the cache counts are inline in practice.
- The one rare edge (a >100KB completion body offloaded to S3, so
  `output.outputBodyJson` is absent) is handled by failing safe: DLQ the record +
  emit `meter.output_body_truncated`, never POST a zeroed-cache (under-billed) row.

Source: AWS-documented Bedrock model-invocation-logging schema (pending dev capture).
Each CloudWatch log event `message` is ONE JSON object of this shape.

| Logical field          | JSON path (CloudWatch record)                                 |
|------------------------|---------------------------------------------------------------|
| requestId              | record["requestId"]                                           |
| modelId (AIP ARN)      | record["modelId"]                                             |
| identity.arn           | record["identity"]["arn"]                                     |
| region                 | record["region"]                                              |
| timestamp              | record["timestamp"]                                           |
| run status / errorCode | record["errorCode"] (absent ⇒ success)                        |
| input_tokens           | record["input"]["inputTokenCount"]  (fallback: output usage)  |
| output_tokens          | record["output"]["outputTokenCount"] (fallback: output usage) |
| cache_read_tokens      | record["output"]["outputBodyJson"]["usage"]["cacheReadInputTokens"]  |
| cache_write_tokens     | record["output"]["outputBodyJson"]["usage"]["cacheWriteInputTokens"] |
| (fallback) input usage | record["output"]["outputBodyJson"]["usage"]["inputTokens"]    |
| (fallback) output usage| record["output"]["outputBodyJson"]["usage"]["outputTokens"]   |

NOTE: pricing stays token × rate via the existing backend rate table — Bedrock
returns token counts, NOT dollars, so the rate table is NOT removed. If a captured
real fixture disagrees with the paths above, the FIXTURE wins — edit this table and
the candidate-path lists in `index.py::_extract` / `_output_usage` to match.
