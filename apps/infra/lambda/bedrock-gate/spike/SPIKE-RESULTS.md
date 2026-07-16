# bedrock-gate spike results — V3 verified GO (2026-07-15)

Throwaway spike proving the one genuinely-novel gateway mechanic before building the rest:
**verbatim binary eventstream passthrough + a usage tee, for both Bedrock request shapes.**
Run against **real dev Bedrock** (Sonnet 4.6 AIP `application-inference-profile/b49y7aodofrc`), not mocks.

## Verdict: GO. Proceed with the amazon-bedrock-provider + custom-endpoint design (spec §4). No fallback needed.

## What was proven

### ConverseStream (OpenClaw provider shape) — PASS
- Client = AWS SDK `BedrockRuntimeClient` with `endpoint` → the proxy + HTTP/1.1 (mirrors OpenClaw's custom endpoint + `AWS_BEDROCK_FORCE_HTTP1=1`). Consumed the full stream: text (451 chars), **thinking/reasoningContent survived** (`sawReasoning: true`), 43 frames.
- Proxy teed a COPY of the byte stream through `@smithy/eventstream-codec`: saw `messageStart / contentBlockDelta / contentBlockStop / messageStop / metadata`, extracted `metadata.usage`.
- **Tee usage == client usage**: both `{input 432, output 119, total 551}`. The tee reads exactly what the client reads.
- **content-type `application/vnd.amazon.eventstream` mirrored verbatim** (load-bearing for ACP).
- `x-amzn-requestid` recovered from response headers.

### InvokeModelWithResponseStream (Claude Code / ACP shape) — PASS
- Anthropic Messages body through the same proxy: 11 chunks parsed clean, text intact ("Hello! …"), content-type mirrored, requestId recovered.
- **Usage source differs by shape** (as the plan noted): Invoke carries `amazon-bedrock-invocationMetrics {inputTokenCount, outputTokenCount}` in the final chunk, not `metadata.usage`. Extracted successfully → Task 5's `UsageTee` must branch on op.

## Gotchas confirmed (fold into Task 5)
1. **HTTP/1.1 is mandatory client-side.** The SDK defaults to HTTP/2; against an HTTP/1.1 proxy it dies `ERR_HTTP2_ERROR: Protocol error`. OpenClaw's `AWS_BEDROCK_FORCE_HTTP1=1` (and the ACP env) are required — already in the config plan. The gateway Lambda Function URL terminates HTTP/1.1/2 at AWS; the client flag is the fix.
2. **Greedy frame decode**: eventstream frames arrive split across TCP chunks — buffer and decode by the 4-byte big-endian total-length prefix (`buffered.readUInt32BE(0)`), emit when `buffered.length >= totalLen`. Proven in `server.mjs`.
3. **Tee must not block the pipe**: write each chunk to the client FIRST, then decode a copy — never gate passthrough on decode success.

## Not exercised (low risk; confirm on Phase-2 live traffic)
- **Cache token fields**: this spike didn't force a cache hit, so `usage.cacheReadInputTokens`/`cacheWriteInputTokens` weren't present. The tee does `JSON.parse(metadata).usage` wholesale, so it captures whatever fields exist — cache fields are the same object, captured by construction. Confirm actual values on the dev-canary's cache-heavy traffic (Phase 2).
- Upstream error passthrough (4xx/5xx body mirroring) — trivial in the real handler; asserted by Task 5 tests.

## Reproduce
```
cd apps/infra/lambda/bedrock-gate/spike && npm install
export AWS_PROFILE=isol8-admin AWS_REGION=us-east-1 SPIKE_PORT=8899
export SPIKE_AIP=arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/b49y7aodofrc
node server.mjs 2>server.log & node client.mjs ; node invoke-client.mjs ; grep tee-result server.log
```

## Library versions (Node v26.3.0)
`@aws-sdk/client-bedrock-runtime` ^3.716, `@smithy/eventstream-codec` ^3.1, `@smithy/signature-v4` ^4.2, `@smithy/node-http-handler`, `@smithy/protocol-http` ^4.1.
