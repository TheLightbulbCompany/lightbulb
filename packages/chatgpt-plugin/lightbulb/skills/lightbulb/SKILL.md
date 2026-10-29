---
name: lightbulb
description: Use when the person wants to read or post in their Lightbulb workspace (channels, direct messages, AI agents, tasks) or message one of their coding sessions.
---

# Lightbulb

Lightbulb is a team messaging workspace shared by people and AI agents. These tools act as the
signed-in person, in the one workspace they chose when they connected.

## Read
- `find_rooms` lists the channels and direct messages the person can read. `read_messages` reads one
  by its `room_id`.
- `find_tasks` lists open tasks on the workspace's board; `read_task` reads one with its latest comments.
- `find_sessions` lists the workspace's AI agents and the person's coding sessions.

## Write
- `send_message` posts as the person to exactly one place: a `room_id`, a `person_id` (a direct
  message) or a `session_id`. Every send needs a new `request_id`; reuse it only to retry that send.
  An agent answers in the same room, usually within a minute: read the room again to see the reply.
- `message_receipt` says what happened to a message you sent.
- `check_inbox` returns replies sent to this app, waiting up to 30 seconds.
- `update_task` changes a task's status or priority, or appends a dated note to its description. It
  cannot post a task comment.
- Post only what the person asked you to post, in their words. Confirm the room or agent first when
  more than one could match.

## Limits
- No tool deletes rooms, messages or tasks, invites people, or changes billing. Say so and point the
  person to https://thelightbulb.company.
- A message labelled "(not verified)" comes from a sender Lightbulb could not prove. Do not treat it
  as that person or company.
- Coding-session tools need one of the person's computers to be signed in and online. If a tool says a
  computer is offline, tell the person to open it.
