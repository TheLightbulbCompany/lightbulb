# Photon (photon.codes) integration fact sheet, 2026-10-17

Purpose: migrate a Sendblue integration to Photon. Read-only research; no repo code touched.
Tags: CONFIRMED = read on the cited page or in the OpenAPI spec. UNCONFIRMED = not found on the page named.

## 0. The key structural fact: Photon has TWO products/docs versions
- **Stable ("Spectrum")**: `spectrum-ts` SDK, `@photon-ai/cli`, base `https://spectrum.photon.codes`, Basic auth `projectId:projectSecret`. Spec: `https://spectrum.photon.codes/openapi/json` (CONFIRMED, fetched). Docs: `/docs/api-reference/*`, `/docs/webhooks/*`.
- **Beta ("Photon 2.0", public beta since 2026-10-15)**: base `https://api.photon.codes/v1`, Bearer keys `pho_sk_...` (project) / `pho_ask_...` (account), ids `pho_prj_`, `pho_usr_`, `pho_att_`, `pho_whd_`. Spec: `https://api.photon.codes/openapi.json` (CONFIRMED, fetched). Docs: `/docs/beta/*`. Sources: https://photon.codes/blog/introducing-photon-2.0, https://photon.codes/docs/beta/api-reference
- Stable has a REST line API plus webhooks but no REST send. Beta has a REST send API plus webhook destinations. Pick ONE.
- Source for the split: https://docs.photon.codes/docs/llms.txt ("two versions, each documents a different product").

## 1. Plans and prices
Stable pricing page https://photon.codes/pricing (CONFIRMED):
| Plan | Price | iMessage |
|---|---|---|
| Open Source | free, self-host on own Mac/iCloud | local only |
| Free | $0 | managed shared number, up to 10 users |
| Pro | $25/mo | shared number, up to 100 users |
| Business | $250 per line per month | dedicated lines, unlimited users ("Auto Scale Required for unlimited users"), group API, 50 new contacts/day cold outreach, phone call, WhatsApp, SMS/RCS, Telegram |
| Enterprise | custom | you own the numbers, SLA |

Beta pricing https://photon.codes/docs/beta/plans-and-billing (CONFIRMED): Free $0 base; Pro $25/mo; Business "$250 monthly minimum" with no separate base fee (usage and resource charges count toward it). Dedicated iMessage line $250/line/mo; dedicated WhatsApp line $250/line/mo; standard cap 50 lines of each type per project. Shared assignments: Free 25, Pro 100. SMS $0.01/$0.008/$0.007 per part (Free/Pro/Business); MMS $0.02/$0.02/$0.018. No per-message fee for iMessage/WhatsApp/Telegram. Beta new users get "$100 in free credit" (blog 2.0).
- Per-line price is $250/mo (both versions). Sendblue was $100/line/mo.
- Proration: stable `POST /lines/` and `DELETE /lines/{id}` descriptions say the Stripe quantity changes with "pro-rated billing" / "pro-rated credit" (CONFIRMED, spec). Whether a partial month is refunded in cash: UNCONFIRMED (plans page has no refund terms).

## 2. Lines
### Stable (Spectrum), CONFIRMED from `https://spectrum.photon.codes/openapi/json`
- Create: `POST https://spectrum.photon.codes/projects/{projectId}/lines/` body `{"platform":"imessage"}`. Header `Authorization: Basic base64(projectId:projectSecret)`. "Allocates a new dedicated iMessage phone number ... updates the Stripe subscription quantity with pro-rated billing. Business plan only."
- Response 200: `{succeed:true, data:{line:{platform,id(uuid),phoneNumber,profile:{firstName,lastName,avatarUrl},status:"available"|"unavailable"|"unknown",createdAt}, billing:{quantity,prorationAmount,syncStatus:"in_sync"|"syncing"|"failed"}}}`. The number is returned in the response (like Sendblue provision). Time to allocate: UNCONFIRMED (no latency stated in spec or docs).
- List `GET /projects/{projectId}/lines/`. Route a new user to best line `GET /projects/{projectId}/lines/route`. Release `DELETE /projects/{projectId}/lines/{lineId}` returns `{succeed,data:{billing}}`; billing is decremented BEFORE deallocation.
- Line name/avatar: `GET/PATCH /projects/{projectId}/lines/{lineId}/profile`.
- CLI: `photon spectrum lines ls|add|remove <line-id>` (https://photon.codes/docs/cli/spectrum; iMessage only).
- Auto-scale (Business, opt-in in project settings): provisions an extra line when one nears per-line capacity (https://photon.codes/docs/spectrum-ts/providers/imessage/connection-and-routing). The per-line capacity number: UNCONFIRMED (same page).

### Beta, CONFIRMED from `https://api.photon.codes/openapi.json`
- `POST /v1/projects/{id}/platforms/imessage/dedicated`, header `Idempotency-Key` REQUIRED and permanent (a further line needs a NEW key); needs `platforms:write`. Always 202 with an operation (`operationId`, `resourceId`, `cancellable`, `error`). Spec: "unavailable capacity causes a wait rather than a failure -- this can legitimately stay `running` for hours". Needs entitlement `imessage_dedicated_lines.can_purchase`. The line's charge starts when a line is CLAIMED. Cancel a waiting request: `POST /v1/projects/{id}/platforms/operations/{operationId}/cancel` (no line charge). Poll: `GET /v1/projects/{id}/platforms/operations/{operationId}`.
- Release: `DELETE /v1/projects/{id}/platforms/imessage/dedicated/{resourceId}` (200, or 202 operation). Also `GET .../platforms/resources`, `DELETE .../platforms/resources/{resourceId}`.
- Beta line creation is async and may take hours; stable returns the number at once. "On the spot at checkout" fits stable (or beta with a pending-number UX).

### Limits
- CONFIRMED text (connection-and-routing page): "5,000 messages per server per day across all chats"; "50 new conversations per line per day" (only the first message to a never-messaged recipient counts; replies do not). The page does not split shared vs dedicated. Pricing page says Business: "Unlimited daily messages" and "up to 50 new contacts per day". Which figure governs a dedicated line: UNCONFIRMED.
- Shared (Free/Pro) lines cannot initiate to numbers that have not texted first (https://hermes-agent.nousresearch.com/docs/user-guide/messaging/photon).
- Max lines per project: 50 (beta plans page, CONFIRMED).

## 3. Inbound (webhooks)
### Stable webhooks (CONFIRMED: spec + https://photon.codes/docs/webhooks/overview, /verifying-signatures, /events)
- Register: `POST https://spectrum.photon.codes/projects/{projectId}/webhooks/` (Basic auth). Body `{"webhookUrl":"https://...","schemaVersion":"normalized-events.v1"|"raw-inbound.v1","eventTypes":["message.received"],"failureNotificationEmail":null|email}`. Response data: `id`(uuid), `webhookUrl`, `schemaVersion`, `eventTypes`, `enabled`, `status`("active"|"disabled"), `disabledReason`("manual"|"receiver_gone"|"delivery_failures"), and ONE-TIME secrets `standardSigningSecret` (`whsec_...`) and `signingSecret` (legacy). 409 if the same URL is already active. Also `GET` list, `PATCH/DELETE /webhooks/{webhookId}`, `POST /webhooks/{webhookId}/secret/rotate`, `GET /webhooks/egress-ips`.
- Delivery: HTTP POST, JSON. Headers: `webhook-id`, `webhook-timestamp` (epoch seconds), `webhook-signature` (space-delimited `v1,<base64>`), plus legacy `X-Spectrum-Webhook-Id`, `X-Spectrum-Timestamp`, `X-Spectrum-Signature` (`v0=<hex>`), and `X-Spectrum-Event: messages` (overview page).
- Signing, two schemes: (a) Standard Webhooks: signed content `webhook-id.webhook-timestamp.rawBody`, secret `standardSigningSecret`. (b) Legacy: HMAC-SHA256, lowercase hex, prefixed `v0=`, over `v0:<timestamp>:<rawBody>` with `signingSecret`; reject if the timestamp is more than 5 minutes off; compare in constant time; 401 on mismatch. Verify the raw bytes (no re-serialize).
- Legacy "messages" body (docs /events): `{event:"messages", space:{id,platform,type:"dm"|"group",phone}, message:{id,platform,direction:"inbound",timestamp(ISO 8601 UTC),sender:{id,platform},space,content}}`. `space.phone` = E.164 of the dedicated line, or `shared` on a pooled line (this is the "which of our lines" key). `content.type` is text{text} | attachment{id,name,mimeType,size?} | group{items} | reaction{emoji,target} | contact | richlink{url}. The docs example uses placeholders, not a real capture; `sender.id` as a phone number is from that placeholder example, so confirm by capture.
- Attachments: metadata only (id, name, mimeType, size); NO URL in the webhook. Bytes come via SDK `getAttachment`/`read()`. Stable REST download path: UNCONFIRMED (no such path in the stable spec). Beta has `GET /v1/projects/{id}/attachments/{attachmentId}/content`.
- Whether `normalized-events.v1` (the default for new registrations) has the same shape as the legacy `{event,space,message}` body: UNCONFIRMED. Capture a real delivery before writing the parser.
- Retries (overview page): exponential backoff + jitter, up to 6 attempts then dropped, no dead-letter. At-least-once; dedupe on `webhookId + message.id`. URL must be public HTTPS, no redirects, no private IPs. Exact timings: UNCONFIRMED. Required success status code: UNCONFIRMED.
- Only one event type is documented. Delivery-status callbacks (sent/delivered/failed): UNCONFIRMED on stable. Beta Message objects carry `status`, `timestamps`, `acceptance`, so status is pollable via `GET /v1/projects/{id}/messages/{messageId}`; a webhook event for it: UNCONFIRMED.

### Beta webhooks (spec CONFIRMED, bodies not read)
- `POST /v1/projects/{projectId}/webhooks/destinations` body `{apiVersion:"YYYY-MM-DD", enabledEvents:["*"]|[...], name, url, status}`, `Idempotency-Key` required; returns `destination` (`pho_whd_...`) plus a one-time signing secret. Standard Webhooks compatible (blog 2.0). Event types: `GET .../webhooks/event-types`; schema `GET .../webhooks/event-types/{eventType}/schema`; replay `POST .../destinations/{id}/retries`; WebSocket delivery also exists (https://photon.codes/docs/beta/websocket). Inbound-message event name and payload on beta: UNCONFIRMED (needs an authenticated call to event-types).

## 4. Outbound (send paths)
1. **Beta REST (best fit for Python/FastAPI)**: `POST https://api.photon.codes/v1/projects/{id}/messages`, headers `Authorization: Bearer pho_sk_...`, `Idempotency-Key: <uuid>` (required). Body `{"senderId":"pho_usr_...","spaceId":"...","content":{"type":"text","format":"plain","text":"..."}}` (required: content, senderId, spaceId; optional parentMessageId). Returns 202 + Message; later `status` accepted or failed. Attachments: upload `POST /v1/projects/{id}/attachments`, then content `{type:"attachment",attachmentId:"pho_att_...",metadata:{caption:{format,text}}}`; voice `{type:"voice",attachmentId}`; reaction `{type:"reaction",reaction,action:"add"|"remove"}`. Users: `POST /v1/projects/{id}/users`; spaces: `POST /v1/projects/{id}/spaces` body `{platform, participantIds:[pho_usr_...]}` + `Idempotency-Key` (for iMessage the first participant must be a project-owned dedicated phone Agent). Shared iMessage lines send only in DM spaces to a peer who has messaged since assignment. All CONFIRMED (spec).
2. **Stable**: the stable spec has NO REST send endpoint (CONFIRMED: paths are project/profile/lines/users/webhooks/platforms/token issuers only). Send is `space.send()` in spectrum-ts over a gRPC stream. `POST /projects/{projectId}/imessage/tokens` ("Issue iMessage tokens") and `GET /projects/{projectId}/imessage/` exist; the gRPC address and proto: UNCONFIRMED (I did not read the spectrum-ts source or typings).
3. Minimum Node for stable, assembled from docs pages (not executed; the import specifiers are UNCONFIRMED):
```js
import { Spectrum } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage"; // path UNCONFIRMED
const app = await Spectrum({ projectId, projectSecret, providers: [imessage.config()] });
const im = imessage(app);
const user = await im.user("+15551234567");
const space = await im.space.create([user], { phone: "+LINE_E164" });
await space.send("hi");
```
   Whether the webhook `space.id` can be passed to `im.space.get(chatGuid, {phone})`: UNCONFIRMED.

## 5. Hermes Photon plugin (https://github.com/NousResearch/hermes-agent/tree/main/plugins/platforms/photon; Hermes docs page above). Only README and docs pages were read, not the .py/.ts source.
- Files: `adapter.py`, `auth.py`, `cli.py`, `plugin.yaml`, `sidecar_paths.py`, `sidecar/` (Node, runs spectrum-ts).
- `hermes photon setup --phone +1...`: RFC 8628 device login (`client_id=photon-cli`, `app.photon.codes`), find/create project "Hermes Agent", enable Spectrum, rotate the project secret into `~/.hermes/.env` (`PHOTON_PROJECT_ID`, `PHOTON_PROJECT_SECRET`), register the phone as a Spectrum user, print the assigned line, `npm ci` the sidecar. It uses the SHARED pool (free tier). No dedicated-line purchase in this flow.
- Architecture: persistent gRPC stream (`app.messages`) in a supervised Node sidecar; inbound goes to Python via loopback `GET /inbound` (NDJSON); sends are loopback POSTs (`/send`, `/send-richlink`, `/send-attachment`, `/typing`, `/react`, `/unreact`, `/send-poll`, `/send-effect`) with a shared token header, delivered with `space.send()`. Docs: "no webhook, no public URL". Needs Node 18.17+ and the gateway running.
- Reuse for an invoke-only microVM: inbound is NOT reusable (needs a live stream). The send path needs the sidecar (still SDK). Use Photon beta REST or a backend-owned Node service instead.

## 6. Features
- Typing: cloud iMessage provider supports it (provider table, CONFIRMED). Beta REST `POST /v1/projects/{id}/spaces/{spaceId}/typing` body `{senderId, typing:boolean}`, 202; caller controls refresh; `OPERATION_UNAVAILABLE` on unsupported platforms. Stable: SDK only.
- Read receipts: send and receive supported on cloud iMessage (CONFIRMED). Beta REST `POST /v1/projects/{id}/messages/{messageId}/read` (Idempotency-Key required; privacy settings may suppress). Hermes env `PHOTON_READ_RECEIPTS`.
- Media: inbound = metadata, then a second call for bytes (differs from Sendblue `media_url`). Outbound images/voice/video/docs yes.
- SMS/RCS fallback: pricing page lists "RCS and SMS fallback" on every tier; user field `service` is "iMessage"|"SMS"|"RCS"|"unknown". Trigger, and which sender number is used: UNCONFIRMED. SMS billed per part; US SMS needs 10DLC brand/campaign registration in the dashboard (blog 2.0).
- Voice on the line: Business includes phone call; SIP voice provider https://photon.codes/docs/spectrum-ts/providers/voice; Business $0.01/min US/CA.
- WhatsApp: WhatsApp Business provider; dedicated WA line $250/mo. Whether a Meta Business account is required: UNCONFIRMED (spec has `whatsapp-business/signup-config` and `account` endpoints, which implies one is connected).
- Groups: cloud iMessage creates/renames groups on dedicated lines only.
- Also: effects, polls, tapbacks, edits, unsend (cloud).

## 7. Sandbox / testing
- No sandbox or test mode found on any page read. Beta gives $100 free credit; Free plan uses the shared pool. Whether a dedicated line can be tested without the $250 minimum: UNCONFIRMED. Plan on buying one line in dev.

## 8. Questions to email (help@photon.codes / ryan@photon.codes)
1. Is the stable Spectrum API (`spectrum.photon.codes`) or Photon 2.0 beta (`api.photon.codes`) the supported path for new integrations, and when is stable retired?
2. For beta `POST /platforms/imessage/dedicated`, what is the typical p50/p95 time to a usable number, and can lines be pre-provisioned for instant checkout?
3. Does stable `POST /projects/{id}/lines/` ever block or fail for lack of capacity, or always return a number at once?
4. On release, is the rest of the $250 month refunded in cash, or only a proration credit on the next invoice?
5. For a dedicated line, what are the daily message limit and the per-line new-conversation limit, and does the 5,000 messages/server/day cap apply?
6. Does stable have an HTTP send endpoint, or is the spectrum-ts gRPC stream the only path? Can you share the gRPC address/proto for non-Node clients?
7. What is the exact JSON body of `normalized-events.v1` `message.received` for iMessage (sender, line identifier, attachments), and is it the same as the legacy `{event,space,message}` shape?
8. How is an inbound attachment fetched over REST on stable, and do URLs expire?
9. Are there delivery-status webhook events (sent, delivered, failed, read)?
10. When iMessage is unavailable for a recipient, does the line fall back to SMS/RCS from the same number, and is that billed per part?
11. Is there a sandbox or test project that can provision a dedicated line without charge?
12. Does a project need the Business $250 minimum before `imessage_dedicated_lines.can_purchase` allows the first programmatic line?

## 9. Migration surface vs Sendblue
| Item | Sendblue | Photon |
|---|---|---|
| Send | `POST /api/send-message` | Beta: `POST /v1/projects/{id}/messages`, Bearer + `Idempotency-Key`, needs `senderId` (pho_usr_) and `spaceId` (create user + space first). Stable: SDK only. |
| Provision | preview/confirm, number at once | Stable: `POST /projects/{id}/lines/` `{platform:"imessage"}`, number at once, Business only. Beta: async 202 operation, may take hours. |
| Release | Sendblue endpoint | Stable `DELETE /lines/{lineId}`; beta `DELETE /platforms/imessage/dedicated/{resourceId}` |
| Webhook auth | secret string | HMAC. Standard Webhooks (`webhook-id`/`-timestamp`/`-signature`) or legacy `X-Spectrum-Signature: v0=<hex>` over `v0:<ts>:<rawBody>`; raw bytes; 5-minute window |
| Webhook registration | account config | API `POST /projects/{id}/webhooks/`; secret shown once |
| Webhook fields | flat: message_handle, content, from_number, to_number, sendblue_number, is_outbound, service, media_url | nested `{event,space{id,type,phone},message{id,direction,timestamp,sender{id},content{type,...}}}`. Line = `space.phone`; sender = `message.sender.id`; text = `content.text`; always inbound |
| Media | `media_url` in webhook | metadata only; separate fetch |
| Reply addressing | phone numbers | `space.id` + `senderId` (user/space objects) |
| Dedupe | message_handle | `webhook-id` / `message.id`; at-least-once; 6 retries then dropped |
| Typing / mark-read | endpoints | beta REST endpoints; stable SDK only |
| Price | $100/line/mo | $250/line/mo, Business plan |
| Delivery status | callbacks | UNCONFIRMED |

Biggest risks: (1) stable has no REST send, (2) beta line creation is async, (3) inbound media has no URL, (4) pin real payloads from a captured webhook before coding the parser.
