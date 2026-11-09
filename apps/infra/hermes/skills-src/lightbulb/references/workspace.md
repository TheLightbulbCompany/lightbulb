# Changing the workspace: `lightbulb_workspace`

Rooms, members and pins change only through this tool. It calls `POST /api/v1/messaging/agent-act` and acts with the rights of **the person whose message started this turn**, so you can do nothing they could not. A routine, task or Paperclip run has no such person and is refused: ask someone in a room to request it.

Why not edit the data yourself: on 18 Oct a hand-written member row skipped the agent's delivery cursor and silenced its @mentions; the tool makes the same call the browser does.

Call it with `action`, `roomId` (the room acted on; omit for `create_room`) and `args`. Inside `args`, camelCase or snake_case field names both work except `pin`/`unpin`, which read `message_id`; `add_member`, `set_listen_mode`, `create_room`, `update_room` reject an unknown key, the others ignore it. A member's `id` is the `id` that `lightbulb_read_rooms` lists for them, not their @handle.

Removing a member needs the person to be a room owner or admin, in any room. In a channel, adding a member and updating the room need it too (except the Activity feed's topic, which any member may update on its own, and a listen-mode change on an existing agent member, which any member may make). In a DM or group chat any participant may add a member at role `member`; in a group chat any member may archive or unarchive it, but renaming it needs its owner. Otherwise the call is refused.

| action | args |
|---|---|
| `add_member`, `set_listen_mode` | `kind` (`human`\|`agent`), `id`, `role` (`admin`\|`member`), `listenMode`, all required. An agent needs `role` `member` and `listenMode` `active`\|`mentions`\|`observe`; a human needs `listenMode` `null` |
| `remove_member` | `kind` (`human`\|`agent`), `id` |
| `create_room` | `name` (required, may be null), `kind` (`channel`\|`dm`\|`group-dm`), `members`: a list of the `add_member` shape (max 256; a `dm` takes exactly one). Omit the person asking (they become the owner); include yourself (kind `agent`, your id) if you need to be in the room. `clientRoomId` is filled in for you |
| `update_room` | any of `name`, `topic`, `archived` (bool) |
| `pin`, `unpin` | `message_id` |

A bad or missing arg comes back as `INVALID_REQUEST` naming the field.


To leave a room yourself, use `remove_member` with kind `agent` and your own id (it needs the person's owner or admin rights).
