# Bedrock invocation-log record — field paths (captured dev, 2026-07-15)

Confirmed against real dev `ConverseStream` records (cache-heavy trial run,
2026-07-15): **all four token counts are TOP-LEVEL** — `input.inputTokenCount`,
`input.cacheReadInputTokenCount`, `input.cacheWriteInputTokenCount`,
`output.outputTokenCount` — and equal the response body's `usage` block
exactly. The body fallback paths in `index.py::_extract` are kept as
defensive-only.

## No bodies — metadata-only delivery

As of 2026-07-15 the logging config disables ALL data delivery
(`textDataDeliveryEnabled` / `embeddingDataDeliveryEnabled` /
`imageDataDeliveryEnabled` = false in `bedrock-meter-stack.ts`), so
`input.inputBodyJson` / `output.outputBodyJson` **no longer appear in
records at all**. Rationale: bodies carry whatever the agent had in context —
live records were observed containing a tenant's Paperclip API key and the
gateway JWT in plaintext — and the meter never needed them (counts are
top-level, above). The `outputBodyJson.usage` rows in the table below are the
historical fallback shape, retained only because the parser still accepts it.

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
