import { EventEmitter } from "node:events";
import { describe, expect, it, vi } from "vitest";
import {
  DEFAULT_SCOPES,
  SHOPIFY_ROUTE_PREFIX,
  createShopifyHandler,
  normalizeScopes,
  normalizeStore,
  parseAuthList,
  parseAuthorizeUrl,
  removeStoreSessions,
  sessionsFilePath,
} from "./shopify.js";

const STORE = "mystore.myshopify.com";
const AUTH_URL = `https://${STORE}/admin/oauth/authorize?client_id=7e9cb568cfd431c538f36d1ad3f2b4f6&scope=read_products&redirect_uri=http%3A%2F%2F127.0.0.1%3A13387%2Fauth%2Fcallback&state=abc&response_type=code&code_challenge=xyz&code_challenge_method=S256`;
const CLI_TEXT = `Shopify CLI will open the app authorization page in your browser.\n\nBrowser did not open automatically. Open this URL manually:\n${AUTH_URL}\n\n`;

/** A child process double: emits on stdout/stderr, exits on demand, records kill(). */
function fakeChild() {
  const child = new EventEmitter() as EventEmitter & {
    stdout: EventEmitter;
    stderr: EventEmitter;
    kill: ReturnType<typeof vi.fn>;
    killed: boolean;
  };
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.killed = false;
  child.kill = vi.fn(() => {
    child.killed = true;
    return true;
  });
  return child;
}

function res() {
  const out: { status?: number; body?: any } = {};
  return {
    out,
    res: {
      setHeader: vi.fn(),
      end: vi.fn((text: string) => {
        out.body = JSON.parse(text);
      }),
      set statusCode(v: number) {
        out.status = v;
      },
      get statusCode() {
        return out.status ?? 200;
      },
    } as any,
  };
}

const req = (method: string, sub: string) => ({ method, url: `${SHOPIFY_ROUTE_PREFIX}${sub}` }) as any;

function harness(opts: { authList?: string; file?: string } = {}) {
  const children: Array<{ args: string[]; child: ReturnType<typeof fakeChild> }> = [];
  const spawn = vi.fn((_cmd: string, args: string[]) => {
    const child = fakeChild();
    children.push({ args, child });
    if (args[0] === "store" && args[1] === "auth" && args[2] === "list") {
      // `store auth list --json` answers synchronously-ish: next tick.
      queueMicrotask(() => {
        child.stdout.emit("data", opts.authList ?? '{"sessions":[]}');
        child.emit("exit", 0);
      });
    }
    return child as any;
  });
  let body: unknown = {};
  const readBody = vi.fn(async () => ({ ok: true, value: body }));
  const files = new Map<string, string>();
  if (opts.file !== undefined) files.set(sessionsFilePath({ XDG_CONFIG_HOME: "/cfg" }), opts.file);
  const readFile = vi.fn(async (file: string) => {
    const text = files.get(file);
    if (text === undefined) throw new Error("ENOENT");
    return text;
  });
  const writeFile = vi.fn(async (file: string, text: string) => {
    files.set(file, text);
  });
  const handler = createShopifyHandler({ spawn, readBody, env: () => ({ XDG_CONFIG_HOME: "/cfg" }), readFile, writeFile });
  return {
    handler,
    spawn,
    children,
    files,
    writeFile,
    setBody: (b: unknown) => {
      body = b;
    },
    lastAuthChild: () => children.filter((c) => c.args[1] === "auth" && c.args[2] !== "list").at(-1)!.child,
  };
}

describe("pure helpers", () => {
  it("parseAuthorizeUrl pulls this store's authorize URL out of the CLI's text", () => {
    expect(parseAuthorizeUrl(CLI_TEXT, STORE)).toBe(AUTH_URL);
    // Another store's URL never counts — the backend opens whatever comes back.
    expect(parseAuthorizeUrl(CLI_TEXT, "other.myshopify.com")).toBeUndefined();
  });

  it("normalizeStore accepts only a bare shop host", () => {
    expect(normalizeStore(" MyStore.myshopify.com ")).toBe(STORE);
    for (const bad of ["evil.com", `${STORE}/admin`, `https://${STORE}`, "169.254.169.254", "", 42, `${STORE}.evil.com`]) {
      expect(normalizeStore(bad)).toBeUndefined();
    }
  });

  it("normalizeScopes defaults to the read set and refuses anything but scope handles", () => {
    expect(normalizeScopes(undefined)).toEqual(DEFAULT_SCOPES);
    expect(normalizeScopes(["read_orders", "read_orders", "read_products"])).toEqual(["read_orders", "read_products"]);
    expect(normalizeScopes([])).toBeUndefined();
    expect(normalizeScopes(["read_orders; rm -rf /"])).toBeUndefined();
    expect(normalizeScopes("read_orders")).toBeUndefined();
  });

  it("sessionsFilePath follows XDG_CONFIG_HOME, then HOME", () => {
    expect(sessionsFilePath({ XDG_CONFIG_HOME: "/home/node/.openclaw/.config" })).toBe(
      "/home/node/.openclaw/.config/shopify-cli-store-nodejs/config.json",
    );
    expect(sessionsFilePath({ HOME: "/home/node" })).toBe("/home/node/.config/shopify-cli-store-nodejs/config.json");
  });

  it("removeStoreSessions drops every key for the store, escaped or not, and keeps the rest", () => {
    const file = JSON.stringify({
      [`7e9c::${STORE}`]: { currentUserId: "1", sessionsByUserId: { "1": { store: STORE } } },
      "7e9c::mystore\\.myshopify\\.com": { currentUserId: "2", sessionsByUserId: {} },
      "7e9c::other.myshopify.com": { currentUserId: "3", sessionsByUserId: {} },
      unrelated: true,
    });
    const { text, removed } = removeStoreSessions(file, STORE);
    expect(removed).toBe(2);
    expect(Object.keys(JSON.parse(text))).toEqual(["7e9c::other.myshopify.com", "unrelated"]);
    // Garbage in, garbage untouched: never rewrite a file we could not parse.
    expect(removeStoreSessions("not json", STORE)).toEqual({ text: "not json", removed: 0 });
  });

  it("parseAuthList reads sessions through banner noise", () => {
    const stdout = `\n╭─ info ─╮\n│ Release notes │\n╰────╯\n{"sessions":[{"store":"${STORE}","scopes":["read_orders"],"expiresAt":"2026-09-17T00:00:00Z","associatedUser":{"id":1,"email":"a@b.c"}}]}\n`;
    expect(parseAuthList(stdout)).toEqual([{ store: STORE, scopes: ["read_orders"], expiresAt: "2026-09-17T00:00:00Z", email: "a@b.c" }]);
    expect(parseAuthList("no json here")).toEqual([]);
  });
});

describe("login route", () => {
  it("spawns store auth without a shell and answers with the authorize URL once printed", async () => {
    const h = harness();
    h.setBody({ store: STORE });
    const r = res();
    const done = h.handler(req("POST", "login"), r.res);
    await new Promise((resolve) => setTimeout(resolve, 10));
    h.lastAuthChild().stdout.emit("data", CLI_TEXT);
    await done;
    expect(h.spawn).toHaveBeenCalledWith(
      "shopify",
      ["store", "auth", "--store", STORE, "--scopes", DEFAULT_SCOPES.join(",")],
      expect.objectContaining({ stdio: ["ignore", "pipe", "pipe"] }),
    );
    expect(r.out.status).toBe(200);
    expect(r.out.body).toEqual({ ok: true, store: STORE, authorizeUrl: AUTH_URL });
  });

  it("refuses a store that is not a bare shop host before spawning anything", async () => {
    const h = harness();
    h.setBody({ store: `${STORE}; rm -rf /` });
    const r = res();
    await h.handler(req("POST", "login"), r.res);
    expect(r.out.status).toBe(400);
    expect(h.spawn).not.toHaveBeenCalled();
  });

  it("reports a CLI that exits without printing a URL", async () => {
    const h = harness();
    h.setBody({ store: STORE });
    const r = res();
    const done = h.handler(req("POST", "login"), r.res);
    await new Promise((resolve) => setTimeout(resolve, 10));
    const child = h.lastAuthChild();
    child.stderr.emit("data", "Error: shopify: command not found\n");
    child.emit("exit", 127);
    await done;
    expect(r.out.status).toBe(502);
    expect(r.out.body.error).toEqual({ code: "LOGIN_START_FAILED", message: "Error: shopify: command not found" });
  });

  it("a second click supersedes a login still waiting for the callback", async () => {
    const h = harness();
    h.setBody({ store: STORE });
    const first = res();
    const p1 = h.handler(req("POST", "login"), first.res);
    await new Promise((resolve) => setTimeout(resolve, 10));
    const c1 = h.lastAuthChild();
    c1.stdout.emit("data", CLI_TEXT);
    await p1;
    const second = res();
    const p2 = h.handler(req("POST", "login"), second.res);
    await new Promise((resolve) => setTimeout(resolve, 10));
    const c2 = h.lastAuthChild();
    expect(c2).not.toBe(c1);
    expect(c1.kill).toHaveBeenCalled();
    c2.stdout.emit("data", CLI_TEXT);
    await p2;
    expect(second.out.status).toBe(200);
  });
});

describe("status route", () => {
  it("is pending while the login child is alive and connected once auth list shows the store", async () => {
    const h = harness({ authList: `{"sessions":[{"store":"${STORE}","scopes":["read_orders"]}]}` });
    h.setBody({ store: STORE });
    const login = res();
    const p = h.handler(req("POST", "login"), login.res);
    await new Promise((resolve) => setTimeout(resolve, 10));
    const child = h.lastAuthChild();
    child.stdout.emit("data", CLI_TEXT);
    await p;
    const pendingRes = res();
    await h.handler(req("GET", `status?store=${STORE}`), pendingRes.res);
    expect(pendingRes.out.body).toMatchObject({ ok: true, store: STORE, pending: true, connected: true });
    child.stdout.emit("data", "Logged in.\n");
    child.emit("exit", 0);
    const doneRes = res();
    await h.handler(req("GET", `status?store=${STORE}`), doneRes.res);
    expect(doneRes.out.body).toMatchObject({ pending: false, connected: true, session: { store: STORE, scopes: ["read_orders"] } });
    expect(doneRes.out.body.error).toBeUndefined();
  });

  it("surfaces the CLI's last line when the login died without a session", async () => {
    const h = harness();
    h.setBody({ store: STORE });
    const p = h.handler(req("POST", "login"), res().res);
    await new Promise((resolve) => setTimeout(resolve, 10));
    const child = h.lastAuthChild();
    child.stdout.emit("data", CLI_TEXT);
    await p;
    child.stderr.emit("data", "Timed out waiting for OAuth callback.\n");
    child.emit("exit", 1);
    const r = res();
    await h.handler(req("GET", `status?store=${STORE}`), r.res);
    expect(r.out.body).toMatchObject({ connected: false, pending: false, error: "Timed out waiting for OAuth callback." });
  });

  it("rejects a missing or foreign store", async () => {
    const h = harness();
    const r = res();
    await h.handler(req("GET", "status?store=evil.com"), r.res);
    expect(r.out.status).toBe(400);
    expect(h.spawn).not.toHaveBeenCalled();
  });
});

describe("logout route", () => {
  it("kills a pending login and drops the store's sessions from the CLI's file", async () => {
    const file = JSON.stringify({ [`7e9c::${STORE}`]: { x: 1 }, "7e9c::other.myshopify.com": { y: 2 } });
    const h = harness({ file });
    h.setBody({ store: STORE });
    const p = h.handler(req("POST", "login"), res().res);
    await new Promise((resolve) => setTimeout(resolve, 10));
    const child = h.lastAuthChild();
    child.stdout.emit("data", CLI_TEXT);
    await p;
    const r = res();
    await h.handler(req("POST", "logout"), r.res);
    expect(child.kill).toHaveBeenCalled();
    expect(r.out.body).toEqual({ ok: true, store: STORE, removed: 1 });
    expect(Object.keys(JSON.parse(h.files.get(sessionsFilePath({ XDG_CONFIG_HOME: "/cfg" }))!))).toEqual(["7e9c::other.myshopify.com"]);
  });

  it("is a no-op when the CLI never wrote a sessions file", async () => {
    const h = harness();
    h.setBody({ store: STORE });
    const r = res();
    await h.handler(req("POST", "logout"), r.res);
    expect(r.out.body).toEqual({ ok: true, store: STORE, removed: 0 });
    expect(h.writeFile).not.toHaveBeenCalled();
  });
});

describe("routing", () => {
  it("404s unknown sub-paths and 405s the wrong verb", async () => {
    const h = harness();
    const a = res();
    await h.handler(req("POST", "nope"), a.res);
    expect(a.out.status).toBe(404);
    const b = res();
    await h.handler(req("GET", "login"), b.res);
    expect(b.out.status).toBe(405);
  });
});
