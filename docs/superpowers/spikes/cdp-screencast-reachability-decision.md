# CDP screencast reachability — the security-sensitive decision

> Companion to `docs/superpowers/plans/2026-08-20-cdp-screencast-liveview.md`.
> This is the load-bearing part: **how the backend reaches the container's CDP
> without exposing raw CDP (unauthenticated full browser control) to the
> network.** The isol8-side relay + panel are built against this decision; the
> container bridge itself is **STOP-flagged for sign-off** (see bottom).

## How the VNC live-view is authenticated today (the pattern we mirror)

Traced in code, not assumed:

1. Frontend noVNC connects to `wss://bv-{env}.isol8.co/api/v1/browser-view/ws`,
   Clerk JWT in the `Sec-WebSocket-Protocol` offer.
2. `bv-{env}` = CloudFront distribution → **VPC origin** → internal ALB `:8081`
   listener (`network-stack.ts` BrowserViewListener), path-capped to
   `/api/v1/browser-view/*`. **CloudFront and the ALB do NOT authenticate** —
   they are a raw WebSocket passthrough into the VPC (the api host is an API
   Gateway HTTP API that cannot carry WS upgrades at all; this is the only raw-WS
   door).
3. ALB `:8081` → backend FastAPI `routers/browser_view.py`. **Auth happens
   HERE**, in Python: validate the Clerk JWT, `resolve_owner_id`, resolve the
   owner's container via Cloud Map. Owner scoping is the whole fence — the
   container is resolved from the authenticated identity, never from client input.
4. The relay connects to `ws://{container_ip}:6080/` (websockify). `6080` binds
   `0.0.0.0` in the container but ingress is fenced: `ContainerFromServiceIngress`
   (`service-stack.ts`) opens **all TCP (0–65535) from the backend service SG
   only** to the container SG. Raw VNC (`5900`) stays loopback (`Xvnc -localhost`).

**The auth is at the backend relay, not at the bv door.** The bv door is an
unauthenticated raw-WS pipe; the Clerk check + owner-scoping live in the FastAPI
route. So any second port fronted the same way inherits the identical protection:
SG-fenced to the backend service, with the backend relay as the only client and
the sole place credentials are checked.

## Key finding: no new SG rule is needed

`ContainerFromServiceIngress` already allows **all TCP ports** from the backend
service SG to the container SG (it exists for "container management"). The
backend can therefore already reach *any* port the container binds on its IP —
including a new CDP-proxy port — and nothing public can. We deliberately add **no
ALB path rule** for the CDP port (unlike VNC's `:8081`), so it has *no* public
door at all — strictly tighter than the VNC path.

## The options (evaluated against the real code)

| Option | What changes | Layer | Verdict |
|---|---|---|---|
| **A. socat TCP proxy** `0.0.0.0:18801 → 127.0.0.1:18800` | one line in the Step C4 boot string (`container-stack.ts`), next to the existing `websockify … 0.0.0.0:6080 127.0.0.1:5900`. `socat` is **already in the extended image** (Dockerfile line 131). Gated on `DISPLAY`, exactly like the rest of C4. | **boot command** (task-def / CDK deploy). No image binary needed. | **RECOMMENDED** |
| B. Baked CDP proxy helper (Node/Python) in the image | a script in `apps/infra/openclaw/` + Dockerfile COPY, launched from C4. Lets us enforce a `Host:` rewrite / restrict to CDP methods. | extended-image **Dockerfile** (image roll → pin flip). | More surface than needed; A already fences at the SG. |
| C. Stream frames through the OpenClaw gateway event channel | a fork RPC that opens the CDP WS *inside* the container, runs `Page.startScreencast`, and streams frames over the existing signed-device WS pool. Zero new network surface; CDP never leaves loopback; rides chat auth. | **fork** change → fork release → image pin. Heaviest. Also abandons the bv door the plan mandates. | Most secure, most expensive; revisit only if A/B prove unstable. |

### Why A is safe (security equivalence argument)

- Raw CDP (`18800`) stays **loopback** — Chrome binds `127.0.0.1` by default;
  we do not change that. socat is the only thing that talks to it.
- The proxy port (`18801`) binds `0.0.0.0` but is reachable **only from the
  backend service SG** (existing all-TCP ingress) — identical to VNC's `6080`,
  and with **no ALB rule**, so no public door exists.
- The backend relay does the **Clerk-JWT + owner-scoping** check before it ever
  connects to `18801` — same fence as `browser_view.py`.
- Chrome's DevTools DNS-rebinding host check accepts **IP** and `localhost`
  Host headers; the backend connects via the container's numeric IP, so the
  auto `Host: {ip}:18801` passes. (Fallback if a stricter Chrome build rejects
  it: send `Host: localhost:18800` — the backend controls the client.)

### The image-pin gate the user asked for

Because the proxy line is part of the `DISPLAY`-gated Step C4 block and (for
option B) the proxy binary lives in the extended image, **only containers on the
new tag have `18801` open.** On old-tag containers the backend relay's TCP
connect to `18801` is refused → the panel shows its disconnected/"asleep" state.
`dev.tag → validate → prod.tag` in `openclaw-version.json` is the staging gate;
no code flag is involved. The frontend screencast panel is inert on old-image
containers and live on new ones — exactly the pin gate.

## STOP-FLAG — the container bridge awaits sign-off

Per the guardrail, I did **not** open any port or ship the container side. The
recommended change (option A) is a **one-line boot-command addition** to
`container-stack.ts` Step C4:

```sh
[ -z "$DISPLAY" ] || { socat TCP4-LISTEN:18801,fork,reuseaddr TCP4:127.0.0.1:18800 & }
```

plus (optionally) a matching comment block documenting the SG fence, and a
`container-stack.test.ts` assertion. No SG change, no Dockerfile change, no fork
change. **This is the piece that needs your review + a live dev measurement**
(multi-client CDP coexistence with the agent's `connectOverCDP`; fps/latency/CPU
vs VNC) before the tag is promoted.
</content>
</invoke>
