# Cards, artifacts and documents: design

Date: 2026-10-16. Status: approved by the owner 2026-10-16. Research behind it: the OpenUI evaluation in this session (facts in the "Mechanisms" section), `docs/research/2026-10-13-employee-voice-and-consumer-agents.md` (the reply rules), and the current Paperclip base `v2026.1012.0-isol8.2` (promoted to prod 2026-10-16, #4188).

Three tracks, one spec, because they meet in the same two places: the message in chat and the right pane.

- **A. Cards in chat.** An agent composes a card from a fixed set of components inside its reply. A click on it answers as the person.
- **B. File artifacts in the pane.** A file the agent made (a spreadsheet, a page, a PDF, a diagram) shows as a card in chat and opens in the right pane in a viewer for its type.
- **C. Documents.** What agents write in a Paperclip document, how it reads in the pane, and comments, versions and editing by people.

Out of scope, by decision: live co-editing (not in Paperclip, not built here); cards that change records on click (a click only posts a message, and sign-offs stay on the approval rail); cards bound to live data through OpenUI's query feature (phase two, after the static cards are in); OpenUI's hosted Gateway, Autofix, slides and report artifacts (inference stays behind the openrouter-gate); image generation; a card in a feed post or in mail (those render a fence as code, by rule).

## 1. Goal

An agent that has a question asks it with options the person can press. An agent that has numbers, a comparison, a plan or a schedule shows it as a card, a table, a chart or a file the person can open, not as a wall of prose. A document an agent writes reads like a colleague's memo, opens in the pane, and the person can comment on a passage, edit it, and see its versions. Nothing an agent draws can act on the person's behalf: every click is a visible message the person chose to send.

## 2. What changes

### A. Cards in chat

**A1. The agent can write a card in its reply.** A reply may carry one fenced block tagged `openui-lang`. The web, desktop and phone render it as the card it describes. A client that cannot render it shows the block as code. A block the parser cannot read renders what it can, so the worst case is plain, not broken.

**A2. The component set.** Twenty names, taken from OpenUI's own library, drawn with our own primitives and our own look:

| Group | Components | What it is for |
|---|---|---|
| Layout and prose | `Stack` (the root), `Text`, `Callout` | Grouping, a line of prose, a highlighted note |
| Showing | `Metric`, `Table` with `Col`, `EditableTable`, `BarChart`, `LineChart`, `Steps`, `Sources`, `Image` | A number with a delta, a small table, a table the person edits and submits, two charts, a plan or progress list, a source list for research, a picture from the message's own attachments |
| Asking | `Choice`, `Buttons` with `Button`, `FollowUps`, `Form` with `Input`, `Select`, `DatePicker` | A question with options (single or multi), labelled replies, suggested replies as chips, a short form, a date to pick |

Everything else in their library waits for a real need.

**A3. A click answers as the person.** A pick, a button, a chip, a form submit or an edited table posts a message in the room as the person, in reply to the card, with the agent mentioned so it wakes as addressed. A single-choice pick posts on selection; a multi pick and a form post on Done. A form posts one `Label: value` line per field; an edited table posts the rows as a markdown table. Once any reply to a card exists, its controls disable and show what was picked. A link in a card opens through the app's own safe-link path. No hidden context ever rides with a click, so a page that prompt-injects an agent can at most draw a button whose text the person reads before pressing it.

**A4. The agent learns it from the skill, not from a per-turn prompt.** A new `references/ui.md` in the bundled lightbulb skill carries the component signatures and when to use each. The skill's always-on rules gain one line: ask with a Choice, show numbers and plans as a card, and a sign-off is still an approval, never a Choice. The signatures in the reference are generated from the one library file, and a test fails when they drift.

**A5. The reply rules ship in the same edit.** The always-on rules gain the "how you reply in chat" block from the 2026-10-13 research: lead with the answer or the action; one to four sentences by default; bullets only for three or more parallel items the reader will act on; no headings in chat; commit to a concrete next step with an owner and a time, or say plainly that you cannot; never offer work you will not do in this turn or put on a task; at most one question, and only if blocked; when done, say "Done" plus the link or the number; match the room's register.

**A6. Documents render cards too.** The same fence renders inside a Paperclip document, so a plan can carry a table, a chart or a step list, and a research document its sources.

### B. File artifacts in the pane

**B1. A file the agent made shows as a card and opens in the pane.** The agent attaches the file in its reply (the existing MEDIA line) or links a file in its workspace. Chat shows a card with the name, the type and the size. A click opens the right pane in the viewer for the type. The Library opens its rows the same way. The dialog that opens documents and attachments today is deleted.

**B2. Viewers, by type.**

| Type | Viewer | Phone |
|---|---|---|
| Spreadsheet (`xlsx`, `csv`) | A grid; one tab per sheet; the first 2,000 rows of each, with a line saying so; view only | Opens in the system viewer |
| HTML page | The page in a sandboxed frame: scripts run, nothing else is allowed, and the frame has no origin of ours | Opens in the system viewer |
| PDF | The pages inline (the embed the dialog already has, moved) | Opens in the system viewer |
| Diagram (a `mermaid` fence) | Drawn inline in the message or document | Shown as code |
| Images, video | Unchanged: inline in the message | Unchanged |
| Markdown, text | Rendered or shown as text, as the dialog does today | Unchanged |

**B3. HTML never runs on our origin.** An HTML attachment is accepted as a new attachment type on the room path. The API serves it as a download, never inline, with a sandbox policy header, and the viewer renders the fetched text inside a frame that has no origin, no forms, no pop-ups and no navigation. So a page the agent wrote, or was tricked into writing, cannot read the app's storage, cannot carry the person's token, and cannot call our API as them.

### C. Documents

**C1. What the agent writes.** A new `references/documents.md` in the lightbulb skill is the first document style guide agents have had. The rules: open with the answer or the decision in one paragraph, not with a timestamp or a title repeated as a heading (the pane shows the title, the author and the date); say who does what by when; name tasks in a table with their links, never as a list of ids; headings only in a document longer than one screen; prose for reasoning, a table for comparisons, a card for numbers and plans, a chart when the shape of the data is the point; no status-report register ("Observed", "Current-state audit"); a plan carries goal, what will be done, who does each part by when, and what is needed from the reader; a research document carries the answer, the evidence, and what to do about it. The paperclip skill's comment style keeps governing comments only.

**C2. The document pane.** A Paperclip document opens in the right pane (the same pane as B), not a modal. It shows the title, the task, who wrote it and when, the revision number, a contents list when the document has four or more headings, the body with cards and diagrams rendered, Comment on a selected passage, the version menu with restore, Edit, and Open task as a secondary action. A document card in chat and a Library row open it. The phone's task screen gets the same header and renders cards in the body.

**C3. Comments on a passage.** Selecting text in the pane offers Comment; the thread shows beside the passage and in the task. These exist (ported from upstream and wired to our routes) and are surfaced in the pane rather than built. A comment on a document reaches the agent the way a task comment does.

**C4. Editing by a person.** Edit opens the document in a markdown editor with headings, lists, tables, code, links, images and @mentions, ported from upstream Paperclip's own editor. Save writes a new revision against the one the person opened; if the document moved under them, the pane says who changed it and offers to reload. A card fence is edited as a code block. The agent sees the person's revision on its next run.

## 3. Mechanisms

### A. Cards

- **Wire format.** The fence is ```` ```openui-lang ```` inside the reply `text`. No new message field; `AgentMessageRequest` and `CanonicalRoomMessage` are untouched. A fence over 16 KB renders as code.
- **Library.** `packages/shared/src/ui/library.ts` defines the twenty components with `@openuidev/lang-core`'s `defineComponent` (name, zod props, description, `component: null`) and `createLibrary({ root: "Stack" })`. Props are positional in zod key order, so key order is part of the contract and the file says so. Web and phone bind renderers to these definitions; they never redeclare props.
- **Web renderer.** `apps/frontend/src/components/messaging/openui/` holds one file per component over `components/ui/*` (shadcn), `recharts` through `components/ui/chart.tsx` for the two charts, and a `library.tsx` that binds them. `roomMarkdown.tsx` gains a `code` override: `language-openui-lang` mounts `<Renderer>` from `@openuidev/react-lang` with `isStreaming={false}`; every other fence falls through to `MarkdownView`'s default. The same override serves document bodies.
- **Phone renderer.** `apps/mobile/src/components/chat/openui/` binds the same names to React Native views; the two charts render as a `Table`. `MessageMarkdown.tsx`'s `fence` rule mounts `<Renderer>` for `openui-lang`.
- **Click to reply.** `onAction` receives `continue_conversation` with `humanFriendlyMessage` and `formState`. The handler builds the reply text (a label; `Label: value` lines; a markdown table) and sends through the room's normal send path with `replyToMessageId` = the card's message, the card's `threadRootMessageId`, and `mentions: [{ kind: "agent", id: <the card's author> }]`. `select_dispatch_targets` already treats a mentioned agent as addressed. `open_url` goes through `safeHref` / `appLinkTarget` like any link.
- **Answered state.** The renderer gets `answered` from the room's loaded messages: the first later human message whose `replyToMessageId` is the card's id AND whose `threadRootMessageId` equals the card's own (a message typed in a top-level card's thread also replies to the card's id, but is not an answer). Controls disable and the picked text shows. Both clients have the room's messages in memory.
- **Image.** `Image(fileName, alt)` resolves `fileName` against the message's own `attachments`; anything else shows the alt text. Room markdown already blocks remote images and this keeps that line.
- **Prompt text.** `packages/shared/scripts/openui-reference.mjs` runs `library.prompt()` and writes the signature block between two marker comments in `apps/infra/hermes/skills-src/lightbulb/references/ui.md`. A vitest test in `packages/shared` regenerates and compares; drift fails CI. `MAINTENANCE.md` lists the anchors. The agent reads the reference on demand; the always-on cost is the one rule line and the reply rules in `SKILL.md`.
- **Dependencies.** `@openuidev/lang-core` in shared; `@openuidev/react-lang` on web and phone (it brings `@openuidev/devtools` and `@openuidev/observability`). pnpm 9 runs dependency postinstall scripts, and lang-core's sends install telemetry, so the four packages go in the root `package.json` `pnpm.neverBuiltDependencies`. No Gateway key, no cloud mode.
- **Reliability signal.** `onError` codes go to `capture("openui.parse_error", { codes, count })` in `lib/analytics.ts`, never content. The owner's call is that the models produce valid fences; this is the one cheap guard.

### B. File artifacts

- **One pane kind.** `PanelKind` gains `artifact`; `PANEL_COMPONENTS.artifact` is `ArtifactDockPanel`; `workspaceController.ts` maps `?panel=artifact&src=…`. `src` is one of `attachment` (`room`, `id`), `paperclip` (the typed artifact id the Library already uses: `document:…`, `work_product:…`, `attachment:…`), or `file` (`agent`, `path`). The file-viewer keeps the workspace tree and mounts the same `ArtifactBody` when a file's extension has a viewer.
- **ArtifactBody.** Fetches bytes through the existing authenticated routes (`/messaging/rooms/{room}/attachments/{id}`, the Teams BFF content routes, the file viewer's read) into an object URL or text, then picks the viewer by content type: spreadsheet, HTML, PDF, image, video, text, markdown, document. `ArtifactViewerDialog.tsx` is deleted; `RoomDocumentCards.tsx`, `RoomImageAttachments.tsx` (non-image files) and `ArtifactsPanel.tsx` call `openPanel("artifact", …)`.
- **Spreadsheet.** SheetJS (`xlsx`, Apache-2.0) parses in the browser from the blob; nothing is parsed on the backend. Sheets as tabs; the first 2,000 rows each through `components/ui/table.tsx`.
- **HTML.** Backend: `text/html` joins `_ATTACHMENT_SIGNATURES` (text sizing, no byte signature) and the content route answers it with `Content-Disposition: attachment` and `Content-Security-Policy: sandbox`. Bridge: `text/html` joins `ATTACHMENT_TYPES` and `_NON_IMAGE_ATTACHMENT_TYPES` in `turn.py`. Viewer: `<iframe sandbox="allow-scripts" referrerpolicy="no-referrer" srcdoc={text}>`, 2 MB cap. No `allow-same-origin`, ever.
- **PDF.** The dialog's object-URL iframe, moved into `ArtifactBody`.
- **Mermaid.** `roomMarkdown.tsx`'s `code` override mounts a `MermaidBlock` for `language-mermaid`: dynamic `import("mermaid")`, `securityLevel: "strict"`, the pattern `LobsterGraphView.tsx` uses. Phone: code.
- **Chat card for a file.** `RoomImageAttachments.tsx` already lists non-image attachments as download chips; the chip becomes the card (name, type, size, Open) and opens the pane instead of downloading. Download stays in the pane header.

### C. Documents

- **Writing rules.** `references/documents.md` as in C1, a routing row in `SKILL.md`, and `chat-vs-document.md` points to it where it says "write the document". The reply rules (A5) go in `SKILL.md`'s non-negotiables.
- **Document view.** `ArtifactBody`'s document branch: header (title, `issue.identifier`, author from `updatedByAgentId` / `updatedByUserId`, `updatedAt`, `latestRevisionNumber`), a contents list built from the rendered headings, the body through `MarkdownView` with the room overrides (cards, mermaid, safe links), `DocumentAnnotationLayer` + `DocumentAnnotationPanel`, `DocumentRevisionMenu`, Edit, Open task. Phone: `teams/issue/[id].tsx` gains the header and the fence rule.
- **Comments.** Already there: `POST /teams/issues/{id}/documents/{key}/annotations` and `…/{thread}/comments`, with the layer and panel in `components/teams/issues/`. The plan verifies on dev that an annotation comment wakes the task's assignee through Paperclip; if the fork does not, that is an open decision, not a silent gap.
- **Editor.** Port `ui/src/components/MarkdownEditor.tsx` from the fork at `v2026.1012.0-isol8.2` (MDXEditor 4.x: headings, lists, table, code block with CodeMirror, link and link dialog, image, quote, thematic break, markdown shortcuts, mention deletion, paste normalization) into `components/teams/shared/components/MarkdownEditor.tsx`, themed with our tokens and Lantern. Save: `PUT /teams/issues/{id}/documents/{key}` with `{ title, format: "markdown", body, changeSummary: "Edited by <name>", baseRevisionId }` where `baseRevisionId` is the revision shown. A conflict answer (the document moved, or a running agent holds its lock) reloads and says who changed it. Mentions in the editor resolve against the company roster the Teams panels already load.
- **Agent side.** Nothing new: Paperclip records the person's revision with `updatedByUserId`, and the next run reads the document as it always has.

## 4. Rollout

Subagent-driven, one PR each, CI green and Codex reviewed before merge, dev on merge, prod on the usual train.

1. **Shared library, generated reference, web renderer, click-to-reply, answered state, parse-error capture.** Inert in prod until an agent writes a fence.
2. **Skill:** `references/ui.md`, `references/documents.md`, the `SKILL.md` rule and reply rules, `MAINTENANCE.md` anchors. Image build, dev pin, a Chrome check on dev, prod pin, `scripts/roll_hermes_runtimes.py`, `scripts/reapply_hermes_skills_dirs.py --all`.
3. **The artifact pane** with the document view, PDF and mermaid; chat cards and the Library open it; the dialog deleted.
4. **Spreadsheet and HTML viewers**, with the backend and bridge accepting `text/html`.
5. **Comments and versions surfaced** in the pane, with the wake verified.
6. **The editor.**
7. **Phone:** the card renderer, the fence rules, the document header, system viewers for files. One OTA.
8. **Phone editing (secondary):** a plain markdown editor on the task screen, saving a revision through the same write with the same conflict handling. One OTA.

## 5. How we confirm it works

On dev, in Chrome, as a founder account, with receipts on each PR:

- Ask an agent in a DM to decide between two plans for you. It answers with a Choice. Press one. The agent continues with that pick, and the card shows it as answered on reload and on the phone.
- Ask for a comparison of three vendors. It answers with a Table, no prose wall. Ask for a plan: a Steps card. Ask for last month's numbers: Metrics.
- Ask for a research document. The chat gets five lines and a card. The card opens the pane: the document opens with the answer, no "Observed" line, tasks in a table. Select a sentence, comment, and the agent wakes and replies in the task.
- Edit the document, save, and ask the agent what changed. It names your change.
- Ask for a spreadsheet of the vendors: a grid opens in the pane. Ask for a landing-page draft: it opens in the frame, and a probe page that tries `document.cookie`, `localStorage` and a fetch to the API gets nothing.
- Ask for a diagram of the funnel: drawn inline.
- Over the first week in prod, `openui.parse_error` under 5% of cards.
- The phone flows are driven in the simulator, not screenshotted.

## 6. Open decisions

1. **Scripts in HTML previews.** Decided 2026-10-16: on, inside the sandbox, so charts and mockups work.
2. **Editing on the phone.** Decided 2026-10-16: in scope as a secondary step after the web editor (rollout step 8): a plain markdown text editor on the task screen, saving through the same revision write; the rich editor stays web-only.
3. **Charts and diagrams on the phone.** Charts fall back to a table and diagrams to code until a native chart library is chosen.
4. **Buttons and FollowUps** both exist because one is an answer and the other a suggestion. Collapse to one if the distinction does not earn its signature line.
5. **Live data cards** (OpenUI queries against an allowlist of our read routes) are phase two.
6. **If an annotation comment does not wake the agent** in the fork, choose: post it as a task comment too, or leave comments as notes for the person.
