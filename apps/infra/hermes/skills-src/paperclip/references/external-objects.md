# External objects (GitHub PRs and issues on a task)

When a GitHub pull request or issue URL is written into an issue's title or
description, a comment, or an issue document, Paperclip tracks it as an
**external object** of that issue and keeps a snapshot of its state (open,
merged, closed, checks failing). The task shows that state in Teams, in the
properties rail and on the task list.

Tracking is automatic: write the full URL
(`https://github.com/<owner>/<repo>/pull/<n>` or `.../issues/<n>`) where it
belongs. **There is no route to add or link an external object by hand** — do
not look for one, and do not invent one.

The feature is a per-instance switch. While it is off, the read below answers
an empty list and nothing is tracked; that is not an error.

## Read what an issue tracks

```bash
curl -sS -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
  "$PAPERCLIP_API_URL/api/issues/<issueId>/external-objects"
```

Needs read access to the issue. Answers an array of
`{object, mentions, mentionCount, sourceLabels}`. The fields worth reading on
`object`: `sanitizedCanonicalUrl`, `displayTitle`, `statusLabel` (e.g.
`Merged`), `statusCategory` (`open` / `waiting` / `running` / `succeeded` /
`failed` / `blocked` / `closed` / `archived` / `auth_required` /
`unreachable` / `unknown`), `liveness` (`fresh` / `stale` / `auth_required` /
`unreachable` / `unknown`) and `lastResolvedAt`.

## It is a snapshot, not a fresh check

The stored state is re-resolved about every five minutes, so it can be that
old. `lastResolvedAt` says when it was last read from GitHub, and a `liveness`
other than `fresh` means do not trust it at all (`auth_required`: the
workspace's GitHub connection cannot see that repository).

So this read is for orientation — what does this task point at, and roughly
where does it stand. Before you **state** a PR's or issue's status, run a
fresh check with the tool that has the answer (the `gh` CLI, a GitHub MCP
tool), as the rule in `SKILL.md` requires.

## Ask Paperclip to re-resolve

```bash
echo '{}' | /app/skills/paperclip/scripts/paperclip-api.sh raw POST \
  "/api/issues/<issueId>/external-objects/refresh"
```

Needs write access to the issue (the same `issue:mutate` boundary as any
other write to it). Optional body `{"objectIds": ["<objectId>", ...]}` (at
most 50) narrows it. Answers `{refreshed: [{object, refreshed, reason}]}`.

It does not force: an object resolved within its window answers
`refreshed: false, reason: "backoff"` with the stored snapshot. It is only
useful to pull a due or stale object forward — it is not the fresh check.
