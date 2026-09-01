/**
 * Validation for MCP servers that run LOCALLY on the user's Mac, driven by
 * the desktop app's `local_mcp_*` Tauri commands.
 *
 * The entries are plain OpenClaw `mcp.servers` config
 * (apps/backend/core/services/openclaw_config.schema.json → mcp.servers), so
 * the shape is shared with the container-side tab. What is NOT shared is the
 * checking: three of the config's failure modes are SILENT at runtime — the
 * server just never produces tools, with no error anywhere the user can see.
 * They are cheap to catch at paste time, so they are caught here:
 *
 *   1. `url` with no `transport` — the client silently picks SSE, not
 *      streamable HTTP, and a streamable-HTTP server then never connects.
 *   2. `localhost` — on OpenClaw's SSRF blocklist. `127.0.0.1` is auto-trusted
 *      and is the same host, so the fix is a literal substitution.
 *   3. `disabled: true` — not the canonical flag; `enabled: false` is.
 *
 * Kept out of the component so the rules have a cheap unit test (the panel
 * itself is a thin shell over invoke()).
 */

export type LocalMcpTransport = "stdio" | "sse" | "streamable-http";

/** One `mcp.servers` entry. Open-ended: OpenClaw accepts more keys than we check. */
export interface LocalMcpServer {
  command?: string;
  args?: string[];
  env?: Record<string, string | number | boolean>;
  cwd?: string;
  url?: string;
  transport?: LocalMcpTransport;
  headers?: Record<string, string | number | boolean>;
  enabled?: boolean;
  [key: string]: unknown;
}

export type ParsedConfig =
  | { ok: true; servers: Record<string, LocalMcpServer> }
  | { ok: false; error: string };

const TRANSPORTS: readonly string[] = ["stdio", "sse", "streamable-http"];

/** Hostnames the agent's SSRF guard rejects, with the equivalent it accepts. */
const BLOCKED_HOSTS: Record<string, string> = {
  localhost: "127.0.0.1",
  "localhost.localdomain": "127.0.0.1",
};

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** The hostname of `url`, lowercased and de-bracketed, or null if unparseable. */
function hostOf(url: string): string | null {
  try {
    return new URL(url).hostname.toLowerCase().replace(/^\[|\]$/g, "");
  } catch {
    return null;
  }
}

/**
 * Parse the paste-a-config textarea into a servers map.
 *
 * Returns the first problem found rather than a list — the input is one JSON
 * blob the user re-edits in place, so a single precise message is what gets
 * acted on.
 */
export function parseLocalMcpConfig(raw: string): ParsedConfig {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { ok: false, error: "Invalid JSON — check for a stray comma or quote." };
  }

  if (!isPlainObject(parsed)) {
    return {
      ok: false,
      error: 'Expected an object of servers, e.g. { "my-server": { "command": "npx", … } }.',
    };
  }

  const entries = Object.entries(parsed);
  if (entries.length === 0) {
    return { ok: false, error: "No servers in that config." };
  }

  for (const [name, entry] of entries) {
    const err = validateEntry(name, entry);
    if (err) return { ok: false, error: err };
  }

  return { ok: true, servers: parsed as Record<string, LocalMcpServer> };
}

/** Returns an error message, or null when the entry is usable. */
function validateEntry(name: string, entry: unknown): string | null {
  if (name.trim() === "") {
    return "A server name can't be empty.";
  }
  if (/\s/.test(name)) {
    return `Server name "${name}" can't contain spaces — try "${name.trim().replace(/\s+/g, "-")}".`;
  }
  if (!isPlainObject(entry)) {
    return `Server "${name}" must be an object.`;
  }

  // Trap 3 — the strict config schema rejects `disabled`; `enabled` is the flag.
  if ("disabled" in entry) {
    return `Server "${name}": "disabled" isn't a valid field. Use "enabled": false instead.`;
  }

  const hasCommand = typeof entry.command === "string" && entry.command.trim() !== "";
  const hasUrl = typeof entry.url === "string" && entry.url.trim() !== "";

  if (!hasCommand && !hasUrl) {
    return `Server "${name}" needs a "command" (a program on your Mac) or a "url" (a remote server).`;
  }
  if ("command" in entry && !hasCommand) {
    return `Server "${name}": "command" must be a non-empty string.`;
  }
  if (
    "args" in entry &&
    !(Array.isArray(entry.args) && entry.args.every((a) => typeof a === "string"))
  ) {
    return `Server "${name}": "args" must be an array of strings.`;
  }
  if ("env" in entry && !isPlainObject(entry.env)) {
    return `Server "${name}": "env" must be an object of NAME: value pairs.`;
  }
  if ("transport" in entry && !TRANSPORTS.includes(entry.transport as string)) {
    return `Server "${name}": "transport" must be one of ${TRANSPORTS.map((t) => `"${t}"`).join(", ")}.`;
  }

  if (hasUrl) {
    const url = (entry.url as string).trim();

    // Trap 1 — no transport means an SSE client, silently. Almost every
    // modern server wants streamable HTTP, so make the choice explicit.
    if (!("transport" in entry)) {
      return `Server "${name}" has a "url" but no "transport" — without it the agent silently uses SSE. Add "transport": "streamable-http" (or "sse" if that's really what this server speaks).`;
    }
    if (entry.transport === "stdio") {
      return `Server "${name}": "transport": "stdio" doesn't go with a "url". Use "streamable-http" or "sse".`;
    }

    const host = hostOf(url);
    if (host === null) {
      return `Server "${name}": "${url}" isn't a valid URL.`;
    }
    // Trap 2 — `localhost` is on the agent's SSRF blocklist; the loopback IP
    // is auto-trusted and reaches the identical server.
    const replacement = BLOCKED_HOSTS[host];
    if (replacement) {
      return `Server "${name}": "${host}" is blocked by the agent's SSRF guard. Use "${replacement}" instead — same server, and it's auto-trusted.`;
    }
  }

  return null;
}
