# Streaming Spark's autocomplete later (a proposal, not built)

Today `POST /api/v1/os/assist/complete` answers in one piece because the front door, API Gateway HTTP API, cannot
stream ("Response streaming: REST Yes / HTTP API No",
docs.aws.amazon.com/apigateway/latest/developerguide/http-api-vs-rest.html). Grey text is about 15 tokens, so
buffering costs little. Tab Tab writes about 400 tokens, so the person waits for the whole reply. Opsis streams the
same reply as server-sent events (`?stream=1`, events `delta`, `done` and `error`; Mac repo teardown 9.12, "The
reply"). This page describes the infrastructure change that would let Spark do the same. Nothing here is
implemented.

## The change

1. **A CloudFront distribution for the assist routes only**, in `apps/infra` (a new construct beside `api-stack.ts`),
   on a new hostname from `domains.ts`, for example `assist[-dev].thelightbulb.company`.
   - **Origin:** a CloudFront VPC origin on the existing internal ALB. VPC origins cost nothing extra. The VPC needs
     an internet gateway, and the AZ use1-az3 is not supported, so check the ALB's subnets first.
   - **Behaviour:** `/api/v1/os/assist/*` only; every other path answers 403 at the edge.
   - **Caching:** the managed `CachingDisabled` policy. Forward `Authorization`, `Content-Type` and `Content-Encoding`
     with an origin request policy. Allow POST.
   - **Keep-alive:** raise the origin keep-alive above its 5 s default, and the origin response timeout to cover Tab Tab.
   - CloudFront forwards a chunked response "as the object is received" (CloudFront developer guide, custom-origin
     behaviour). TLS ends at the nearest edge.
2. **The ALB** accepts the VPC origin's security group, as it accepts the VPC Link's today.
3. **The backend:** with `?stream=1`, the route answers `text/event-stream` with a `StreamingResponse`. It sends one
   `delta` per clause (`insert`, `chunks`, `id`, `locked_words`), then `done` with the same `result` as today's whole
   answer. Without `?stream=1` it answers as now, so the API Gateway path keeps working. Starlette cancels the generator
   when the client disconnects, which closes the provider stream.
4. **The Mac** points its assist calls at the new hostname.

## The client-IP fix (security, must ship with it)

The research named an `X-Lightbulb-Client-Ip` header that API Gateway overwrote. That header is gone from the code.
Today the client IP is `request.client.host`: uvicorn's `ProxyHeadersMiddleware` walks `X-Forwarded-For` from the
right, trusting only the VPC CIDRs (`main.py`), and every per-IP limit (`_PER_IP` in `routers/os.py`) keys on it.

Through CloudFront the chain becomes viewer → CloudFront (appends the viewer's IP to any `X-Forwarded-For` the
viewer sent) → VPC origin ENI (a VPC address, trusted) → ALB (appends the ENI). The first untrusted entry from the
right is then CloudFront's record of the viewer, which is correct. Two things must hold, and a test must pin both:

- **Only CloudFront may append before the ALB.** Add a CloudFront Function on viewer request that deletes any
  incoming `X-Forwarded-For` and any `X-Lightbulb-*` header, so a caller cannot plant entries. The
  middleware's right-to-left walk already ignores them, so this is a second layer.
- **The VPC-origin ENIs sit in the trusted CIDRs** (10.0.0.0/16 dev, 10.2.0.0/16 prod). If they ever sit outside,
  every request would look like it came from CloudFront, and per-IP limits would lock everyone out together.

## What it costs and what it loses

- No fixed cost for the VPC origin; CloudFront's request and data-out prices apply to this path only.
- API Gateway's stage throttling (`throttlingBurstLimit` / `throttlingRateLimit`) does not cover the new path. The
  route's own daily ceiling still holds. Add AWS WAF rate rules on the distribution if abuse shows.
- Whether CloudFront and the ALB pass a client abort upstream is undocumented. Test it on dev before relying on
  cancel-to-stop-billing.

## Alternatives the research weighed

- A second, public ALB: one hop less, about $16 a month fixed.
- API Gateway REST API streaming: a migration of the front door, at $3.50 per million requests.
- Not Lambda: Python response streaming is not native, and a streamed response keeps billing after the client
  disconnects.
