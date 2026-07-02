# Hermes v0.14.0 OAuth Provider Proxy — Verification (audit open question #6)

**Date:** 2026-06-16
**Source read:** Hermes v0.14.0 (`v2026.5.16`) at `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/hermes-agent`
**Prior design:** `git show c9562d39:docs/audit-2026-05-17-hermes-vs-openclaw.md` §5.4 / open question #6 (cached `/tmp/hermes-audit.md` lines 783–786, 819, 1169, 1184)

**Verdict in one line: NON-STARTER for our hosted multi-tenant topology, and the audit OVER-SOLD it on two counts.** The shipped feature is a localhost-only, single-tenant, single-credential dev convenience wrapping exactly ONE provider (Nous Portal) — not "Claude Pro / ChatGPT Pro / SuperGrok" as the release notes and the prior audit both claim.

---

## 1. What it is, exactly

`hermes proxy start` runs a minimal aiohttp HTTP server that speaks the OpenAI wire protocol on `http://127.0.0.1:8645/v1/...` and forwards each request to an upstream inference API, **swapping the client's `Authorization` header for the operator's own OAuth-minted bearer**. It does not transform bodies — it's a credential-attaching forwarder (`hermes_cli/proxy/server.py:1-10`, `:119-202`).

The problem it solves (per the module docstring, `hermes_cli/proxy/__init__.py:1-16`): let external local apps ("OpenViking, Karakeep, Open WebUI") ride the user's already-logged-in subscription **instead of copy-pasting a static API key into each app's config**. Run the proxy once, point any OpenAI-compatible client at `localhost`, send any dummy bearer (it's discarded), and the proxy attaches the real credential and auto-refreshes it near expiry.

Mechanics that pin down the design:
- **Default bind is loopback:** `DEFAULT_HOST = "127.0.0.1"`, `DEFAULT_PORT = 8645` (`server.py:52-53`).
- **One process = one upstream:** the adapter is selected once at `proxy start` and baked into the app via `web.AppKey("adapter", ...)` (`server.py:96-97`, `cli.py:39-68`). There is no per-request routing to different credentials.
- **Path allowlist:** only `/chat/completions`, `/completions`, `/embeddings`, `/models` are forwarded; everything else 404s (`adapters/nous_portal.py:32-39`, `server.py:124-131`).
- **Credential source is a single global file:** the only registered adapter reads `~/.hermes/auth.json` via `_load_auth_store()` → `get_hermes_home() / "auth.json"` (`adapters/nous_portal.py:18-25,112-122`; `hermes_cli/auth.py:780`), refreshes the Nous OAuth state, and mints the Nous `agent_key` (`adapters/nous_portal.py:74-105`).

### CONTRADICTION — loudly flag this

The v0.14.0 RELEASE notes (`RELEASE_v0.14.0.md:14`, `:190`) and the prior audit (`/tmp/hermes-audit.md:783-784`) both state the proxy wraps **"Claude Pro, ChatGPT Pro, SuperGrok."** The shipped code does NOT. The adapter registry has exactly one entry:

```python
# hermes_cli/proxy/adapters/__init__.py:15-17
ADAPTERS: Dict[str, Type[UpstreamAdapter]] = {
    "nous": NousPortalAdapter,
}
```

Default provider is `nous` (`cli.py:39`). There is an `UpstreamAdapter` ABC (`adapters/base.py:38-91`) so Claude/ChatGPT/Grok adapters *could* be added later, but as of v0.14.0 **only Nous Portal is implemented**. Anyone planning around "proxy our ChatGPT-OAuth subscriptions through this" is planning around vaporware in this release.

---

## 2. Hosted multi-tenant usability — NO

It is single-tenant / local-dev by construction. Three independent code facts each make it unusable as "one proxy serving many isolated per-user containers":

1. **One global credential per process.** The Nous adapter resolves credentials from the single installation-wide `~/.hermes/auth.json` (`adapters/nous_portal.py:112-122`; `auth.py:780`). The inbound `Authorization` header is explicitly stripped (`server.py:48` lists `authorization` as hop-by-hop, replaced at `:151`). So the proxy has **no way to tell tenant A from tenant B** — every request gets the same operator's bearer. A shared central proxy would cross-bill and cross-leak every user onto one subscription. That is the opposite of our per-user isolation requirement.

2. **No auth on the listener.** Any bearer is accepted and discarded (`__init__.py:7-10`). The only "security" is binding to loopback (`server.py:53`). Expose it on `0.0.0.0` and it becomes an open credential-lending relay for whoever's `auth.json` it holds. There is no API-key check, no tenant token, no allowlist of callers.

3. **Per-container co-location buys nothing.** Even if we ran one proxy *inside each* per-user Hermes container (so isolation comes from the container, not the proxy), the proxy still just reads that container's own `auth.json` and forwards to it. The container's model client can already read `auth.json` directly — the proxy adds a localhost hop and a second moving part for zero benefit in our topology. The proxy's value (avoid copy-pasting a key into N *separate local apps* that share one human's subscription) does not map to "one container, one tenant, one credential."

---

## 3. Could it simplify provider-auth wiring (OpenClaw or Hermes)? — NO, recommend dropping it from scope

The audit's §5.4 hope (lines 784–786) was: *"if this proxy can be hosted alongside the container or pointed at a centralized internal proxy, the per-container file dance goes away — the container sees a plain bearer-authed HTTP endpoint, and OAuth lifecycle moves to the proxy."*

That hope does not survive contact with the code:

- **It cannot be the centralized internal proxy** — it has no per-tenant credential dimension (point 2.1) and no caller auth (point 2.2). To make it multi-tenant you would have to rewrite the adapter to key credentials by an inbound tenant token, add a real auth layer, and add a secret-store backend instead of `~/.hermes/auth.json`. At that point you have written a new service and are not "using the Hermes proxy" in any meaningful sense.
- **It does not remove the OAuth file dance for OpenClaw.** OpenClaw's auth model (per memory `project_openclaw_oauth_auth_model`: single-default-agent source, 6.6 per-agent WAL SQLite, seed via container-global `credentials/oauth.json`) is unrelated and would be untouched. The proxy is a Hermes CLI subcommand reading a Hermes auth store; it has no awareness of OpenClaw's seeding path.
- **For the Hermes runtime itself it is redundant.** A Hermes container already authenticates to its provider from its own `auth.json` (`auth.py:780`, Docker bootstrap `RELEASE_v0.14.0.md:132` "Bootstrap auth.json from env on first boot"). Inserting a localhost proxy in front of the same `auth.json` adds a hop, not a simplification.

**Recommendation:** Close audit open question #6 as **rejected**. The OAuth Provider Proxy is a local-dev convenience for sharing one human's subscription across several desktop apps; it is single-tenant, loopback-bound, unauthenticated, and (in v0.14.0) Nous-only. It does NOT simplify our hosted provider-auth wiring for either OpenClaw or Hermes, and treating it as a "unified replacement for the two-file dance" (audit line 819) was based on release-note marketing that the code contradicts.

The genuinely reusable artifact here is the **`UpstreamAdapter` ABC pattern** (`adapters/base.py`) — if we ever build our *own* centralized provider proxy, that credential-resolve/refresh/allowed-paths contract is a clean shape to copy. But that's "borrow a 90-line interface," not "adopt the feature."

---

## File:line index (everything cited was read this session)

- `RELEASE_v0.14.0.md:14, :190` — release-note claim (Claude/ChatGPT/SuperGrok) — CONTRADICTED by code
- `hermes_cli/proxy/__init__.py:1-16` — purpose: local apps share user's subscription; any bearer discarded
- `hermes_cli/proxy/cli.py:30-74` — `proxy start`: default provider `nous`, single adapter baked in
- `hermes_cli/proxy/server.py:48,52-53,96-97,119-202` — loopback default, auth header stripped+replaced, one adapter per app
- `hermes_cli/proxy/adapters/__init__.py:15-17` — registry: `{"nous": NousPortalAdapter}` only
- `hermes_cli/proxy/adapters/base.py:38-91` — `UpstreamAdapter` ABC (the reusable bit)
- `hermes_cli/proxy/adapters/nous_portal.py:18-25,32-39,74-122` — reads global `~/.hermes/auth.json`, Nous-only path allowlist
- `hermes_cli/auth.py:780` — `get_hermes_home() / "auth.json"` = single installation-wide credential store
