# Bedrock invocation-log record — field paths (CloudWatch-only meter)

> ## PRE-FLIP GATE — confirm against a REAL cached-Converse record before `METER_IS_LIVE=true`
>
> The field paths below match the AWS-documented Bedrock model-invocation-logging
> schema and the synthetic fixture in `fixtures/inline_record.json`, but the EXACT
> shape MUST be confirmed against **one real dev record** before the meter goes
> live. The cache-token path is the single highest-risk unknown (a wrong path
> silently zeroes the ~30× cache undercount the meter exists to fix). The parser
> in `index.py` is DEFENSIVE (multi-path `_first_int` extraction + a top-level →
> output-body fallback) so a documented shape change degrades rather than silently
> mis-bills — but do not flip the meter live on synthetic ground truth.
>
> Before flipping `METER_IS_LIVE` to `true`, someone with dev AWS access MUST:
> 1. `aws sso login --profile isol8-admin`; pick a live dev `$OWNER` + its AIP ARN.
> 2. Temporarily enable model-invocation logging in DEV (manual, account+region-global —
>    NOT via the per-env stack; tear it down after).
> 3. Generate ONE cached-Converse run (cache-heavy turn so the cache counts are
>    non-zero) and pull its record from CloudWatch Logs.
> 4. ALSO confirm the rare edge: a run whose COMPLETION body exceeds 100KB. When
>    that happens the output body is offloaded to S3, `output.outputBodyJson` is
>    absent, and the cache counts can't be read — the Lambda DLQs the record +
>    emits `meter.output_body_truncated` rather than POSTing an under-billed
>    cache=0 row. Confirm this is rare in practice (the cached INPUT prompt is the
>    big thing, and we never need it; the output body is normally <100KB inline).
> 5. Overwrite `fixtures/inline_record.json` with the REAL record (scrub prompt/
>    output TEXT to `"<redacted>"`, but **NEVER zero the token/cache numbers** —
>    the fixture MUST keep its real non-zero `cacheReadInputTokens` or the cache
>    test is vacuous).
> 6. Reconcile the table below + the candidate-path tuples in
>    `index.py::_extract` / `_output_usage` against the captured shape — the
>    **FIXTURE wins** over any candidate guess.
> 7. Tear down the temporary dev logging config (leave nothing account-global behind).
>
> Then delete this warning block, change the title to "(captured dev, <date>)", and
> re-run `python -m pytest test_index.py -v`.

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
