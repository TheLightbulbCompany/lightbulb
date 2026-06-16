/**
 * In-memory ring buffer of the most recent backend request IDs.
 *
 * Every backend response carries an `X-Request-ID` header (set by the FastAPI
 * RequestContextMiddleware). `api.ts` records each one here on both success and
 * failure. When the user files a support ticket, the dialog attaches the most
 * recent IDs so the triaging agent can grep the exact CloudWatch log lines for
 * the requests leading up to the problem — not just the one that surfaced an
 * error toast.
 *
 * Module-scoped (one buffer per tab). Not persisted; resets on reload.
 */

const MAX = 20;
const buffer: string[] = [];

/** Record a request ID. No-ops on null/empty and on a consecutive duplicate. */
export function recordRequestId(id: string | null | undefined): void {
  if (!id) return;
  if (buffer[buffer.length - 1] === id) return; // a single retry can repeat one
  buffer.push(id);
  if (buffer.length > MAX) buffer.shift();
}

/** The recorded IDs, newest first, capped at {@link MAX}. */
export function getRecentRequestIds(): string[] {
  return [...buffer].reverse();
}

/** Test/reset helper. */
export function clearRequestIds(): void {
  buffer.length = 0;
}
