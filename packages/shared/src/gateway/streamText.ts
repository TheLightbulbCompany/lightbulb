/**
 * Cumulative-buffer reconstruction for streaming text frames.
 *
 * The backend streams `chunk` / `thinking` frames in two shapes (see
 * ChatIncomingMessage in ./types):
 *   - append deltas (`append: true`) — `content` extends the run's text;
 *   - full-text replacement (no `append`) — `content` is the CURRENT
 *     assistant message's full text. Upstream resets its accumulator at
 *     every assistant message_start (i.e. after every tool call), so
 *     mid-run this is SEGMENT-scoped, not run-cumulative. Sent as the
 *     first frame per (connection, run), for OpenClaw `replace: true`
 *     events, and as the chat.final backstop, so a client that missed
 *     deltas (reconnect, dropped frame) self-corrects.
 *
 * This helper's replace branch is therefore only correct as-is while no
 * tool call has split the run. A caller tracking a committed segment
 * offset must rebuild non-append frames as frozen-prefix + content
 * itself (see the mobile agent screen's chunk handler) instead of
 * folding them through here.
 */
export function accumulateStreamText(
  prev: string | null,
  msg: { content: string; append?: boolean },
): string {
  if (msg.append) {
    return (prev ?? "") + msg.content;
  }
  return msg.content;
}
