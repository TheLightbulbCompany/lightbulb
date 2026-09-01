import { describe, expect, it } from "vitest";

import { parseLocalMcpConfig } from "../localMcp";

/**
 * The three traps in lib/localMcp.ts fail SILENTLY at runtime — a wrong
 * config produces a server that never yields tools and never errors anywhere
 * the user can see. This suite is what keeps them caught at paste time.
 */
describe("parseLocalMcpConfig", () => {
  const err = (raw: string) => {
    const r = parseLocalMcpConfig(raw);
    if (r.ok) throw new Error(`expected a rejection, got ${JSON.stringify(r.servers)}`);
    return r.error;
  };

  describe("trap 1 — a url without a transport silently gets an SSE client", () => {
    it("rejects a url entry with no transport, naming streamable-http", () => {
      const message = err('{"remote":{"url":"https://mcp.example.com/mcp"}}');
      expect(message).toContain('no "transport"');
      expect(message).toContain("streamable-http");
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

  describe("trap 2 — localhost is SSRF-blocked, 127.0.0.1 is auto-trusted", () => {
    it("rejects localhost and names the replacement", () => {
      const message = err('{"local":{"url":"http://localhost:8080/mcp","transport":"streamable-http"}}');
      expect(message).toContain("localhost");
      expect(message).toContain("127.0.0.1");
    });

    it("catches localhost regardless of case or path", () => {
      expect(err('{"local":{"url":"HTTP://LocalHost:3000/sse","transport":"sse"}}')).toContain("127.0.0.1");
    });

    it("accepts the loopback IP", () => {
      expect(parseLocalMcpConfig('{"local":{"url":"http://127.0.0.1:8080/mcp","transport":"streamable-http"}}').ok).toBe(
        true,
      );
    });

    it("does not flag a hostname that merely contains 'localhost'", () => {
      expect(parseLocalMcpConfig('{"x":{"url":"https://localhost.example.com/mcp","transport":"sse"}}').ok).toBe(true);
    });

    it("rejects a url that is not a url at all", () => {
      expect(err('{"x":{"url":"not a url","transport":"sse"}}')).toContain("isn't a valid URL");
    });
  });

  describe("trap 3 — `disabled` is rejected by the strict schema; `enabled` is the flag", () => {
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
