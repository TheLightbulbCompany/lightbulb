import { describe, expect, it } from "vitest";

import { parseLocalMcpConfig } from "../localMcp";

/**
 * The traps in lib/localMcp.ts fail SILENTLY at runtime — a wrong config
 * produces a server that never yields tools and never errors anywhere the user
 * can see. This suite is what keeps them handled at paste time.
 *
 * `localhost` is deliberately NOT rejected: the SSRF blocklist never runs for a
 * configured MCP server URL (see the module header), the Rust side doesn't
 * reject it either, and it is the most common local-MCP case.
 */
describe("parseLocalMcpConfig", () => {
  const err = (raw: string) => {
    const r = parseLocalMcpConfig(raw);
    if (r.ok) throw new Error(`expected a rejection, got ${JSON.stringify(r.servers)}`);
    return r.error;
  };

  describe("a url without a transport silently gets an SSE client", () => {
    it("fills in streamable-http rather than rejecting — the Rust side defaults it too", () => {
      expect(parseLocalMcpConfig('{"remote":{"url":"https://mcp.example.com/mcp"}}')).toEqual({
        ok: true,
        servers: { remote: { url: "https://mcp.example.com/mcp", transport: "streamable-http" } },
      });
    });

    it("leaves an explicit sse alone — the default only fills a gap", () => {
      expect(parseLocalMcpConfig('{"remote":{"url":"https://mcp.example.com/mcp","transport":"sse"}}')).toEqual({
        ok: true,
        servers: { remote: { url: "https://mcp.example.com/mcp", transport: "sse" } },
      });
    });

    it("does not add a transport to a command entry", () => {
      expect(parseLocalMcpConfig('{"fs":{"command":"npx"}}')).toEqual({
        ok: true,
        servers: { fs: { command: "npx" } },
      });
    });

    it("accepts the same entry once transport is explicit", () => {
      expect(
        parseLocalMcpConfig('{"remote":{"url":"https://mcp.example.com/mcp","transport":"streamable-http"}}'),
      ).toEqual({
        ok: true,
        servers: { remote: { url: "https://mcp.example.com/mcp", transport: "streamable-http" } },
      });
    });

    it("rejects stdio paired with a url", () => {
      expect(err('{"remote":{"url":"https://x.example.com/mcp","transport":"stdio"}}')).toContain(
        "doesn't go with a \"url\"",
      );
    });

    it("rejects a transport outside the schema's enum", () => {
      expect(err('{"remote":{"url":"https://x.example.com/mcp","transport":"http"}}')).toContain(
        '"transport" must be one of',
      );
    });

    it("does not demand a transport from a stdio (command) entry", () => {
      expect(parseLocalMcpConfig('{"fs":{"command":"npx","args":["-y","pkg"]}}').ok).toBe(true);
    });
  });

  describe("localhost is NOT rejected — the SSRF blocklist never runs for a configured MCP url", () => {
    it("passes a localhost url through untouched", () => {
      expect(parseLocalMcpConfig('{"local":{"url":"http://localhost:3000/mcp","transport":"streamable-http"}}')).toEqual(
        {
          ok: true,
          servers: { local: { url: "http://localhost:3000/mcp", transport: "streamable-http" } },
        },
      );
    });

    it("defaults a transport-less localhost url like any other", () => {
      expect(parseLocalMcpConfig('{"local":{"url":"http://localhost:3000/mcp"}}')).toEqual({
        ok: true,
        servers: { local: { url: "http://localhost:3000/mcp", transport: "streamable-http" } },
      });
    });

    it("accepts the loopback IP too — neither host is special", () => {
      expect(parseLocalMcpConfig('{"local":{"url":"http://127.0.0.1:8080/mcp","transport":"sse"}}').ok).toBe(true);
    });
  });

  describe("`disabled` is rejected by the strict schema; `enabled` is the flag", () => {
    it("rejects `disabled` and points at `enabled`", () => {
      const message = err('{"fs":{"command":"npx","disabled":true}}');
      expect(message).toContain('"disabled"');
      expect(message).toContain('"enabled": false');
    });

    it("rejects `disabled: false` too — the key itself is what breaks", () => {
      expect(err('{"fs":{"command":"npx","disabled":false}}')).toContain('"enabled": false');
    });

    it("accepts `enabled: false`", () => {
      expect(parseLocalMcpConfig('{"fs":{"command":"npx","enabled":false}}').ok).toBe(true);
    });
  });

  describe("server keys", () => {
    it("rejects whitespace in a key and suggests the hyphenated form", () => {
      expect(err('{"my server":{"command":"npx"}}')).toContain('"my-server"');
    });

    it("rejects an empty key", () => {
      expect(err('{"":{"command":"npx"}}')).toContain("can't be empty");
    });

    // Without an explicit check this is a SILENT DROP, not a visible error:
    // JSON.parse gives `__proto__` as an own property (no setter invoked), so
    // it survives Object.entries — but the accumulator is a plain `{}`, so
    // `servers["__proto__"] = entry` writes the PROTOTYPE and no own property
    // appears. The user would save a config missing that server, with nothing
    // shown. Assert the error, and assert the entry never materializes.
    it("rejects a __proto__ key instead of silently dropping the server", () => {
      expect(err('{"__proto__":{"command":"npx"}}')).toContain("reserved");
    });

    it("does not let a __proto__ key reach the saved map", () => {
      const parsed = parseLocalMcpConfig('{"__proto__":{"command":"npx"},"ok":{"command":"npx"}}');
      expect(parsed.ok).toBe(false);
      // Belt and braces: the prototype of a fresh object is untouched.
      expect(Object.prototype.hasOwnProperty.call({}, "command")).toBe(false);
    });
  });

  describe("shape", () => {
    it("rejects invalid JSON", () => {
      expect(err('{"fs":{"command":"npx",}}')).toContain("Invalid JSON");
    });

    it("rejects a non-object top level", () => {
      expect(err('[{"command":"npx"}]')).toContain("Expected an object of servers");
    });

    it("rejects an empty map", () => {
      expect(err("{}")).toContain("No servers");
    });

    it("rejects a url that is not a url at all", () => {
      expect(err('{"x":{"url":"not a url","transport":"sse"}}')).toContain("isn't a valid URL");
    });

    it("rejects an entry with neither command nor url", () => {
      expect(err('{"fs":{"args":["-y"]}}')).toContain('needs a "command"');
    });

    it("rejects a non-string args array", () => {
      expect(err('{"fs":{"command":"npx","args":[1,2]}}')).toContain('"args" must be an array of strings');
    });

    it("rejects a non-object env", () => {
      expect(err('{"fs":{"command":"npx","env":"API_KEY=x"}}')).toContain('"env" must be an object');
    });

    it("accepts a full stdio entry with env", () => {
      const result = parseLocalMcpConfig(
        '{"fs":{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/Users/me"],"env":{"API_KEY":"k"}}}',
      );
      expect(result.ok).toBe(true);
    });
  });
});
