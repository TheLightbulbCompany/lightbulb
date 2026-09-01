/**
 * Validation for MCP servers that run LOCALLY on the user's Mac, driven by
 * the desktop app's `local_mcp_*` Tauri commands.
 *
 * The entries are plain OpenClaw `mcp.servers` config
 * (apps/backend/core/services/openclaw_config.schema.json → mcp.servers), so
 * the shape is shared with the container-side tab. What is NOT shared is the
 * checking:
 *
 *   - `url` with no `transport` is NORMALIZED, not rejected. Left alone the
 *     client silently picks SSE, so this fills in `streamable-http` — the same
 *     default the desktop host applies (upstream `applyMcpConnectionOverride`).
 *     Writing it here only makes the value explicit and visible in the list;
 *     rejecting it would refuse a config the Rust side happily accepts.
 *   - `disabled: true` is rejected — not the canonical flag; `enabled: false` is.
 *   - server keys must be non-empty and whitespace-free.
 *
 * NOT checked: `localhost`. It IS in OpenClaw's BLOCKED_HOSTNAMES, but that
 * check never runs for a configured MCP server URL — `mcp-transport.ts` builds
 * the fetch via `buildMcpHttpFetch({resourceUrl})` →
 * `ssrfPolicyFromHttpBaseUrlAllowedOrigin`, and `resolveSsrfPolicyForUrl`
 * (src/infra/net/ssrf.ts) copies the matching origin's hostname into
 * `allowedHostnames`, which makes `shouldSkipPrivateNetworkChecks` true and
 * skips BOTH `assertAllowedHostOrIpOrThrow` (pre-DNS) and
 * `assertAllowedResolvedAddressesOrThrow` (post-DNS). The configured origin is
 * trusted whichever it is — `127.0.0.1` is not special — so
 * `http://localhost:3000/mcp` connects fine and must validate. The Rust side
 * does not reject it either; blocking it here would break the single most
 * common local-MCP case.
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

/** What a url-only entry becomes — matches the desktop host's own default. */
export const DEFAULT_URL_TRANSPORT: LocalMcpTransport = "streamable-http";

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
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

  const servers: Record<string, LocalMcpServer> = {};
  for (const [name, entry] of entries) {
    const err = validateEntry(name, entry);
    if (err) return { ok: false, error: err };

    const server = entry as LocalMcpServer;
    // A url-only entry would silently get an SSE client. Pin the transport the
    // desktop host would have defaulted to anyway, so it is explicit in the
    // saved config and shows up on the server's card.
    servers[name] =
      server.url && !server.transport ? { ...server, transport: DEFAULT_URL_TRANSPORT } : server;
  }

  return { ok: true, servers };
}

/** Returns an error message, or null when the entry is usable. */
function validateEntry(name: string, entry: unknown): string | null {
  if (name.trim() === "") {
    return "A server name can't be empty.";
  }
  if (/\s/.test(name)) {
    return `Server name "${name}" can't contain spaces — try "${name.trim().replace(/\s+/g, "-")}".`;
  }
  // Must be rejected explicitly, not just because the desktop host also
  // reserves it. `parseLocalMcpConfig` accumulates into a plain `{}`, so
  // `servers["__proto__"] = entry` sets the object's PROTOTYPE rather than
  // creating an own property: the server would vanish from the saved config
  // with no error shown and no entry written. Catch it here so the user gets
  // a message instead of a silent drop.
  if (name === "__proto__") {
    return `Server name "__proto__" is reserved — pick another name.`;
  }
  if (!isPlainObject(entry)) {
    return `Server "${name}" must be an object.`;
  }

  // The strict config schema rejects `disabled`; `enabled` is the flag.
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

    if (entry.transport === "stdio") {
      return `Server "${name}": "transport": "stdio" doesn't go with a "url". Use "streamable-http" or "sse".`;
    }
    try {
      new URL(url);
    } catch {
      return `Server "${name}": "${url}" isn't a valid URL.`;
    }
  }

  return null;
}
