/** A same-origin path to come back to (an absolute path, never `//host` or `/\host`), else /voice (Home, which shows setup until it is finished). */
export function safeReturnTo(raw: string | null): string {
  return raw && raw.startsWith("/") && !raw.startsWith("//") && !raw.startsWith("/\\") ? raw : "/voice";
}
