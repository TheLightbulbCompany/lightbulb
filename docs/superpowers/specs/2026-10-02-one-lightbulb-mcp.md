# One Lightbulb MCP: a session posts as itself, a person posts as themselves

Owner decision 2026-10-02 ("the Claude Code session should post as itself"). Delete this spec when the work ships; the PRs are the record.

## Rule

- A coding session (Claude Code, Codex) that Lightbulb can **attest** posts **as that session**, through the owner's approval card. Attestation exists only on a Mac running the Lightbulb app (peer process check, device token).
- A client Lightbulb **cannot attest** posts under a **client identity derived from its OAuth client**, never as the signed-in person and never as a particular session. The consented `lightbulb:messages:write` scope is its authority; the signed-in person stays the accountable principal on the row.
- A **person** posts as themselves only from Lightbulb's own surfaces (web, Mac, phone). No MCP tool posts as a human any more.
- The user sees **one server name, `lightbulb`, and one tool set**, whichever path serves it.

## Surfaces

| Where the client runs | Server entry | Who serves it | Author of a send | Approval |
| --- | --- | --- | --- | --- |
| Mac with the Lightbulb app, sharing enabled | `lightbulb` (stdio, installed by enrollment) | the local bridge (`lightbulb-desktop session-mcp`) | the attested session (`kind: system`, session id, "Claude Code · <title>") | owner approval card, as today |
| Anywhere else | `lightbulb` (HTTP, `https://api.thelightbulb.company/mcp`) | the hosted MCP | a client identity (`kind: system`, id `mcp:<user_id>:<client>`, "Claude Code via <person>") | none; consented write scope |

Enrollment replaces the `lightbulb_native_sessions` entry with `lightbulb`; if a `lightbulb` HTTP entry already exists in the user-scoped config, enrollment replaces it and says so in the setup status ("Mac bridge is serving `lightbulb`"). Disable restores nothing (the person re-adds the HTTP entry if they want it without the app); the setup copy says that.

## Tools (identical names and schemas on both servers)

`find_rooms`, `find_sessions`, `read_messages`, `send_message(room_id | person_id | session_id, text, request_id, reply_to_message_id?)`, `message_receipt(request_id)`. The local bridge drops `list_targets`/`send`/`receipt` (the sharing skill text is updated in the same PR). `find_sessions` on the local bridge marks the calling session (`isThisSession: true`) so the model never addresses itself.

## Backend

1. **Reads for the local bridge.** New routes under `/api/v1/local-sessions/device/…` (`rooms`, `rooms/{room_id}/messages`, `sessions`, `receipt`) authenticated by the paired-device token (`machine_auth`) and resolved to the enrolled human through the enrollment row; they call the same `messaging`/`local_sessions` service functions the hosted tools call, with that human's `AuthContext`. Read-only; a device token never reaches a write route. This is the one new grant: a paired Mac may read what its human may read. Fails closed when the enrollment is disabled, paused or revoked.
2. **Hosted sends author as the client.** `hosted_mcp.send_message` derives the client from the access token's `client_id` (CIMD URL → `claude-code`, `codex`, else the URL host) and writes `author = {kind: "system", id: "mcp:<user_id>:<client>", displayName: "Claude Code via <person display name>"}`, `via: "lightbulb_mcp"`, `actorUserId: <user_id>` (new field, the accountable human). `create_human_message` grows a `author_override` used only here; membership, idempotency, mentions and the agent-reply cap are unchanged. Dispatch treats it like any system-authored post (the DM agent may answer; an `@agent` mention is honoured).
3. **Receipts** keep working by `request_id` for both authors.

## Desktop (local bridge)

- `native_mcp::configure` installs the server as `lightbulb` (user scope) for Claude Code and Codex, removing `lightbulb_native_sessions` and any pre-existing `lightbulb` entry it did not create (recorded in the setup status).
- `session-mcp` exposes the five tools; `find_rooms`/`read_messages`/`find_sessions`/`message_receipt` call the new device routes with the short-lived device token; `send_message` is the existing approval-gated native send, with `person_id`/`session_id` mapped onto the existing destination forms.
- Setup status rows read "Serving `lightbulb` for new sessions".

## Acceptance

- Mac: fresh enable shows one `lightbulb` entry in `claude mcp list` and `codex mcp list`; a new Claude Code session calls `find_rooms` (reads as the human) and `send_message` (parks an approval; approved post is authored by the session); disable leaves no `lightbulb` entry.
- Non-Mac: Claude Code over OAuth sends and the row shows author "Claude Code via <person>", not the person; receipt resolves; the person's own surfaces still post as the person.
- No MCP path can author as a human; no device token can reach a write route.
