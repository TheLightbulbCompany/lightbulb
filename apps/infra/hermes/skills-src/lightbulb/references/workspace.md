# Changing the workspace: `lightbulb_workspace`

Rooms, members, pins and search change only through this tool. It calls `POST /api/v1/messaging/agent-act` and acts with the rights of **the person whose message started this turn**, so you can do nothing they could not. A routine, task or Paperclip run has no such person and is refused: ask someone in a room to request it.

Why not edit the data yourself: on 18 Oct a hand-written member row skipped the agent's delivery cursor and silenced its @mentions; the tool makes the same call the browser does.

Call it with `action`, `roomId` (the room acted on; omit for `create_room` and the searches) and `args`. Inside `args`, camelCase or snake_case field names both work except `pin`/`unpin`, which read `message_id`; an unknown key is rejected. In a channel, adding or removing a member and updating the room need the person to be a room owner or admin, otherwise the call is refused (except a listen-mode change on an existing agent member, or adding a member at role `member` to a DM or group chat, which any participant may do).

| action | args |
|---|---|
| `add_member`, `set_listen_mode` | `kind` (`human`\|`agent`), `id`, `role` (`admin`\|`member`), `listenMode`, all required. An agent needs `role` `member` and `listenMode` `active`\|`mentions`\|`observe`; a human needs `listenMode` `null` |
| `remove_member` | `kind` (`human`\|`agent`), `id` |
| `leave_room` | none |
| `create_room` | `name` (required, may be null), `kind` (`channel`\|`dm`\|`group-dm`), `members`: a list of the `add_member` shape (max 256, omit yourself and the person asking; a `dm` takes exactly one). `clientRoomId` is filled in for you |
| `update_room` | any of `name`, `topic`, `archived` (bool) |
| `pin`, `unpin` | `message_id` |
| `search_messages` | `q` (1-200 chars), optional args field `room_id`, `limit` (1-50, default 30) |
| `search_sessions` | `q` (1-200 chars), `limit` (1-50, default 30) |

A bad or missing arg comes back as `INVALID_REQUEST` naming the field.
