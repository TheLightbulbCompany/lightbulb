# Bedrock invocation-log record — field paths (SYNTHETIC fixtures, NOT yet confirmed)

> ## ⚠️ BUILD-GATE NOT YET SATISFIED — confirm against a REAL dev record before `METER_IS_LIVE=true`
>
> The three fixtures in `fixtures/` (`inline_record.json`, `s3_offloaded_record.json`,
> `s3_body.json`) and the field paths in the table below are **SYNTHETIC** — hand-built
> from the AWS-documented Bedrock model-invocation-logging schema, **NOT captured from a
> real dev invocation**. They were created under an autonomous run with no live AWS/SSO
> access, so the plan's Task 1 live-capture steps (1–7) could not be executed.
>
> **These paths are UNVERIFIED.** The cache-token paths and the S3-offload reference field
> are the two highest-risk unknowns in the whole meter (the #1 correctness landmine: a wrong
> cache path silently zeroes the ~30× cache undercount the meter exists to fix). The parser
> in `index.py` is written DEFENSIVELY (tries each candidate path, falls back to S3) so a
> shape mismatch degrades rather than silently mis-bills — but the meter MUST NOT go live on
> synthetic ground truth.
>
> **Before flipping `METER_IS_LIVE` to `true` (Plan 4 cutover), someone with dev AWS access
> MUST run the original Task 1 live-capture (plan steps 1–7):**
> 1. `aws sso login --profile isol8-admin`; pick a live dev `$OWNER` + its AIP ARN.
> 2. Temporarily enable model-invocation logging in DEV (manual, account+region-global — NOT
>    via the per-env stack; tear it down after).
> 3. Generate a short run (inline record) AND a >100KB-cached-prefix run (S3-offloaded record).
> 4. Pull one inline + one offloaded record from CloudWatch Logs.
> 5. Overwrite the three fixtures with the REAL records (scrub prompt/output TEXT to
>    `"<redacted>"`, but **NEVER zero the token/cache numbers** — the offloaded `s3_body.json`
>    MUST keep its real non-zero `cacheReadInputTokens` or the merge test is vacuous).
> 6. Reconcile the table below + the candidate-path tuples in
>    `index.py::_extract` / `_find_s3_ref` / `_merge_s3_body` against the captured shape — the
>    **FIXTURE wins** over any candidate guess.
> 7. Tear down the temporary dev logging config (leave nothing account-global behind).
>
> Then delete this warning block, change the title to "(captured dev, <date>)", and re-run
> `python -m pytest test_index.py -v`.

Source: AWS-documented Bedrock model-invocation-logging schema (SYNTHETIC; pending dev capture).
Each CloudWatch log event `message` is ONE JSON object of this shape.

| Logical field          | JSON path (inline)                           | JSON path (S3-offloaded body)            |
|------------------------|----------------------------------------------|------------------------------------------|
| requestId              | record["requestId"]                          | (same — always in the CW record)         |
| modelId (AIP ARN)      | record["modelId"]                            | (same)                                   |
| identity.arn           | record["identity"]["arn"]                    | (same)                                   |
| region                 | record["region"]                             | (same)                                   |
| timestamp              | record["timestamp"]                          | (same)                                   |
| run status / errorCode | record["errorCode"] (absent ⇒ success)       | (same)                                   |
| input_tokens           | record["input"]["inputTokenCount"]           | s3_body["usage"]["inputTokens"]          |
| output_tokens          | record["output"]["outputTokenCount"]         | s3_body["usage"]["outputTokens"]         |
| cache_read_tokens      | record["input"]["cacheReadInputTokenCount"]  | s3_body["usage"]["cacheReadInputTokens"] |
| cache_write_tokens     | record["input"]["cacheWriteInputTokenCount"] | s3_body["usage"]["cacheWriteInputTokens"]|
| S3-offload reference   | record["input"]["inputBodyS3Path"] (s3:// URI) | n/a                                    |

NOTE: the paths above are the AWS-documented CANDIDATE shape, mirrored into the SYNTHETIC
fixtures so the parser's primary candidate path matches them. If a captured real fixture
disagrees, the FIXTURE wins — edit this table and the candidate-path lists in
index.py::_extract / _find_s3_ref / _merge_s3_body to match.
