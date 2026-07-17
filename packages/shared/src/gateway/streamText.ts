/**
 * Cumulative-buffer reconstruction for streaming text frames.
 *
 * The backend streams `chunk` / `thinking` frames in two shapes (see
 * ChatIncomingMessage in ./types):
 *   - append deltas (`append: true`) — `content` extends the run's text;
 *   - full-text replacement (no `append`) — `content` IS the run's text.
 *     Sent as the first frame per (connection, run), for OpenClaw
 *     `replace: true` events, and as the chat.final backstop, so a client
 *     that missed deltas (reconnect, dropped frame) self-corrects.
 *
 * Clients keep one accumulated buffer per stream and fold every frame
 * through this helper; downstream logic then sees exactly the cumulative
 * text the legacy protocol always delivered.
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
