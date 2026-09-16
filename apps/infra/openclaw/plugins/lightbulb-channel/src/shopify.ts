// The Connections door for the baked Shopify CLI (see /app/skills/shopify and the
// Dockerfile's @shopify/cli layer). `shopify store auth` runs a PKCE grant against
// the owner's store with Shopify's own CLI app, but its redirect is hard-wired to
// http://127.0.0.1:13387/auth/callback — loopback on whichever host runs the CLI.
// So the login can only start HERE, inside the container, and only complete in the
// container's Chrome (the owner drives that through the Browser panel). The backend
// has no exec RPC into the gateway, which is why this is a plugin route rather than
// a `container_rpc.call`: the backend POSTs `login`, gets the authorize URL back,
// opens it in Chrome via `browser.request`, and polls `status` until the CLI has
// stored a session. `logout` drops that session again on disconnect.
//
// Every route is `auth: "gateway"` — only the backend, holding the container's
// gateway token, can reach it. Inputs are fenced (bare `<shop>.myshopify.com`,
// `[a-z_]` scopes) and the CLI is spawned WITHOUT a shell, so nothing here can be
// turned into command injection by a crafted store name.
import type { ChildProcess } from "node:child_process";
import type { IncomingMessage, ServerResponse } from "node:http";
import { homedir } from "node:os";
import path from "node:path";

export const SHOPIFY_ROUTE_PREFIX = "/plugins/lightbulb-channel/shopify/";
/** Mirrors the backend's normalize_store fence: the permanent shop host, nothing else. */
export const STORE_RE = /^[a-z0-9][a-z0-9-]{0,59}\.myshopify\.com$/;
const SCOPE_RE = /^[a-z_]{1,64}$/;
export const DEFAULT_SCOPES = ["read_products", "read_orders", "read_customers", "read_inventory", "read_locations", "read_reports"];
/** `store auth` prints its URL within a second or two; well past that, the CLI is broken, not slow. */
export const URL_WAIT_MS = 20_000;
/** The CLI itself gives Shopify five minutes for the callback; finished logins are forgotten after this. */
export const LOGIN_TTL_MS = 6 * 60_000;
const LIST_TIMEOUT_MS = 20_000;
const MAX_BODY_BYTES = 16_384;
const POLL_MS = 50;

export type SpawnLike = (
  command: string,
  args: string[],
  options: { env: NodeJS.ProcessEnv; stdio: ["ignore", "pipe", "pipe"] },
) => ChildProcess;
type ReadBody = (params: {
  req: IncomingMessage;
  res: ServerResponse;
  maxBytes: number;
  timeoutMs: number;
  invalidJsonMessage: string;
}) => Promise<{ ok: boolean; value?: unknown }>;

export type ShopifyDeps = {
  spawn: SpawnLike;
  readBody: ReadBody;
  env: () => NodeJS.ProcessEnv;
  readFile: (file: string, encoding: "utf8") => Promise<string>;
  writeFile: (file: string, text: string, encoding: "utf8") => Promise<void>;
  now?: () => number;
};

type LoginState = {
  store: string;
  child: ChildProcess;
  output: string;
  authorizeUrl?: string;
  exited: boolean;
  exitCode: number | null;
  startedAt: number;
};

export type StoredSession = {
  store: string;
  scopes: string[];
  expiresAt?: string;
  refreshTokenExpiresAt?: string;
  email?: string;
};

function json(res: ServerResponse, status: number, body: Record<string, unknown>): true {
  res.statusCode = status;
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(JSON.stringify(body));
  return true;
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export function normalizeStore(raw: unknown): string | undefined {
  if (typeof raw !== "string") return undefined;
  const store = raw.trim().toLowerCase();
  return STORE_RE.test(store) ? store : undefined;
}

/** Undefined for anything but a non-empty list of `[a-z_]` scope handles; absent → the read set. */
export function normalizeScopes(raw: unknown): string[] | undefined {
  if (raw === undefined || raw === null) return [...DEFAULT_SCOPES];
  if (!Array.isArray(raw) || raw.length === 0) return undefined;
  const scopes = raw.map((s) => (typeof s === "string" ? s.trim() : ""));
  return scopes.every((s) => SCOPE_RE.test(s)) ? Array.from(new Set(scopes)) : undefined;
}

/** The URL `store auth` prints after "Open this URL manually:" — pinned to THIS store's authorize path. */
export function parseAuthorizeUrl(output: string, store: string): string | undefined {
  const re = new RegExp(`https://${store.replace(/\./g, "\\.")}/admin/oauth/authorize\\?\\S+`);
  return re.exec(output)?.[0];
}

export function loginSucceeded(output: string): boolean {
  return /Logged in\./.test(output);
}

function lastLine(output: string): string {
  const lines = output
    .split("\n")
    .map((l) => l.replace(/\x1b\[[0-9;]*m/g, "").trim())
    .filter((l) => l && !/^[╭╰│─]/.test(l));
  return lines[lines.length - 1] ?? "";
}

/** conf's file for the CLI's `shopify-cli-store` project: $XDG_CONFIG_HOME/<project>-nodejs/config.json. */
export function sessionsFilePath(env: NodeJS.ProcessEnv): string {
  const base = env.XDG_CONFIG_HOME?.trim() || path.join(env.HOME?.trim() || homedir(), ".config");
  return path.join(base, "shopify-cli-store-nodejs", "config.json");
}

/**
 * The CLI keys a store's sessions `${clientId}::${store}` (cli-kit escapes the dots
 * for its dot-notation store, so the key may carry `\.` or plain `.` on disk —
 * both are matched). Every key for the store goes, other stores stay untouched.
 */
export function removeStoreSessions(fileText: string, store: string): { text: string; removed: number } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(fileText);
  } catch {
    return { text: fileText, removed: 0 };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return { text: fileText, removed: 0 };
  const escaped = store.replace(/\\/g, "\\\\").replace(/\./g, "\\.");
  const out = parsed as Record<string, unknown>;
  let removed = 0;
  for (const key of Object.keys(out)) {
    const sep = key.indexOf("::");
    if (sep < 0) continue;
    const tail = key.slice(sep + 2);
    if (tail === store || tail === escaped) {
      delete out[key];
      removed += 1;
    }
  }
  return { text: JSON.stringify(out, null, "\t"), removed };
}

/** `store auth list --json` → sessions. Tolerates banner text around the JSON. */
export function parseAuthList(stdout: string): StoredSession[] {
  const start = stdout.indexOf("{");
  const end = stdout.lastIndexOf("}");
  if (start < 0 || end <= start) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(stdout.slice(start, end + 1));
  } catch {
    return [];
  }
  const sessions = (parsed as { sessions?: unknown })?.sessions;
  if (!Array.isArray(sessions)) return [];
  return sessions.flatMap((s) => {
    if (!s || typeof s !== "object") return [];
    const row = s as Record<string, unknown>;
    if (typeof row.store !== "string") return [];
    const user = row.associatedUser as Record<string, unknown> | undefined;
    return [
      {
        store: row.store,
        scopes: Array.isArray(row.scopes) ? row.scopes.filter((x): x is string => typeof x === "string") : [],
        ...(typeof row.expiresAt === "string" ? { expiresAt: row.expiresAt } : {}),
        ...(typeof row.refreshTokenExpiresAt === "string" ? { refreshTokenExpiresAt: row.refreshTokenExpiresAt } : {}),
        ...(user && typeof user.email === "string" ? { email: user.email } : {}),
      },
    ];
  });
}

export function createShopifyHandler(deps: ShopifyDeps) {
  const now = deps.now ?? Date.now;
  const logins = new Map<string, LoginState>();

  function reap(): void {
    for (const [store, st] of logins) {
      if (st.exited && now() - st.startedAt > LOGIN_TTL_MS) logins.delete(store);
    }
  }

  function runCli(args: string[], timeoutMs: number): Promise<{ code: number | null; stdout: string; stderr: string }> {
    return new Promise((resolve) => {
      const child = deps.spawn("shopify", args, { env: deps.env(), stdio: ["ignore", "pipe", "pipe"] });
      let stdout = "";
      let stderr = "";
      let settled = false;
      const finish = (code: number | null) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve({ code, stdout, stderr });
      };
      const timer = setTimeout(() => {
        child.kill();
        finish(null);
      }, timeoutMs);
      child.stdout?.on("data", (chunk) => {
        stdout += String(chunk);
      });
      child.stderr?.on("data", (chunk) => {
        stderr += String(chunk);
      });
      child.on("error", () => finish(null));
      child.on("exit", (code) => finish(code));
    });
  }

  async function login(store: string, scopes: string[]): Promise<{ ok: true; authorizeUrl: string } | { ok: false; message: string }> {
    reap();
    const existing = logins.get(store);
    if (existing && !existing.exited) existing.child.kill(); // a re-click supersedes the earlier wait
    const child = deps.spawn("shopify", ["store", "auth", "--store", store, "--scopes", scopes.join(",")], {
      env: deps.env(),
      stdio: ["ignore", "pipe", "pipe"],
    });
    const st: LoginState = { store, child, output: "", exited: false, exitCode: null, startedAt: now() };
    logins.set(store, st);
    const onData = (chunk: unknown) => {
      st.output += String(chunk);
      if (!st.authorizeUrl) st.authorizeUrl = parseAuthorizeUrl(st.output, store);
    };
    child.stdout?.on("data", onData);
    child.stderr?.on("data", onData);
    child.on("exit", (code) => {
      st.exited = true;
      st.exitCode = code;
    });
    child.on("error", (err) => {
      st.exited = true;
      st.exitCode = -1;
      st.output += `\n${err.message}`;
    });
    const deadline = now() + URL_WAIT_MS;
    while (!st.authorizeUrl && !st.exited && now() < deadline) await sleep(POLL_MS);
    if (st.authorizeUrl) return { ok: true, authorizeUrl: st.authorizeUrl };
    if (!st.exited) child.kill();
    return { ok: false, message: lastLine(st.output) || "shopify store auth printed no authorize URL" };
  }

  async function status(store: string): Promise<{ connected: boolean; pending: boolean; error?: string; session?: StoredSession }> {
    reap();
    const st = logins.get(store);
    const pending = st !== undefined && !st.exited;
    const failed = st !== undefined && st.exited && !loginSucceeded(st.output);
    const list = await runCli(["store", "auth", "list", "--json"], LIST_TIMEOUT_MS);
    const session = parseAuthList(list.stdout).find((s) => s.store === store);
    return {
      connected: session !== undefined,
      pending,
      ...(failed && !session ? { error: lastLine(st.output) || `shopify store auth exited with ${st.exitCode}` } : {}),
      ...(session ? { session } : {}),
    };
  }

  async function logout(store: string): Promise<{ removed: number }> {
    const st = logins.get(store);
    if (st && !st.exited) st.child.kill();
    logins.delete(store);
    const file = sessionsFilePath(deps.env());
    let text: string;
    try {
      text = await deps.readFile(file, "utf8");
    } catch {
      return { removed: 0 }; // never logged in on this volume
    }
    const next = removeStoreSessions(text, store);
    if (next.removed > 0) await deps.writeFile(file, next.text, "utf8");
    return { removed: next.removed };
  }

  return async (req: IncomingMessage, res: ServerResponse): Promise<boolean> => {
    const url = new URL(req.url ?? "/", "http://plugin.invalid");
    const sub = url.pathname.startsWith(SHOPIFY_ROUTE_PREFIX) ? url.pathname.slice(SHOPIFY_ROUTE_PREFIX.length) : "";
    const method = (req.method ?? "GET").toUpperCase();
    if (sub === "status") {
      if (method !== "GET") {
        res.setHeader("Allow", "GET");
        return json(res, 405, { ok: false, error: { code: "INVALID_REQUEST", message: "GET only" } });
      }
      const store = normalizeStore(url.searchParams.get("store"));
      if (!store) return json(res, 400, { ok: false, error: { code: "INVALID_REQUEST", message: "store must be a bare <shop>.myshopify.com domain" } });
      return json(res, 200, { ok: true, store, ...(await status(store)) });
    }
    if (sub !== "login" && sub !== "logout") {
      return json(res, 404, { ok: false, error: { code: "NOT_FOUND", message: "unknown shopify route" } });
    }
    if (method !== "POST") {
      res.setHeader("Allow", "POST");
      return json(res, 405, { ok: false, error: { code: "INVALID_REQUEST", message: "POST only" } });
    }
    const body = await deps.readBody({ req, res, maxBytes: MAX_BODY_BYTES, timeoutMs: 10_000, invalidJsonMessage: "invalid JSON body" });
    if (!body.ok) return true;
    const value = (body.value ?? {}) as Record<string, unknown>;
    const store = normalizeStore(value.store);
    if (!store) return json(res, 400, { ok: false, error: { code: "INVALID_REQUEST", message: "store must be a bare <shop>.myshopify.com domain" } });
    if (sub === "logout") return json(res, 200, { ok: true, store, ...(await logout(store)) });
    const scopes = normalizeScopes(value.scopes);
    if (!scopes) return json(res, 400, { ok: false, error: { code: "INVALID_REQUEST", message: "scopes must be a non-empty list of scope handles" } });
    const result = await login(store, scopes);
    if (result.ok) return json(res, 200, { ok: true, store, authorizeUrl: result.authorizeUrl });
    return json(res, 502, { ok: false, store, error: { code: "LOGIN_START_FAILED", message: result.message } });
  };
}
