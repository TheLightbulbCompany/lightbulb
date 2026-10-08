# Managing A User's Inbox

Archive an issue out of a user's Mine inbox with `/app/skills/paperclip/scripts/paperclip-api.sh raw POST /api/issues/<issueId>/inbox-archive` (no body needed for the normal case — Paperclip resolves the responsible user from your run context), and reverse it with `raw DELETE /api/issues/<issueId>/inbox-archive`. Targeting another user explicitly (`{"userId": "..."}` on stdin) requires that user's saved opt-in policy or an `inbox:manage` grant — the implicit default-open policy for a user who has never touched the control does not extend to that. Both mutations carry `X-Paperclip-Run-Id` automatically, like every other helper write.

Archive only when the issue is truly resolved for that user — never while they are still expected to review, approve, answer, choose, or decide something. It is reversible and audited, but that does not make premature cleanup acceptable.
