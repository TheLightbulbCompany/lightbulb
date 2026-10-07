import { describe, it, expect } from "vitest";
import {
  resolveProviderStatus,
  statusToDotColor,
  type AuthStatusProvider,
} from "../llm-auth-status";

function providers(...entries: AuthStatusProvider[]): AuthStatusProvider[] {
  return entries;
}

describe("statusToDotColor", () => {
  it("maps ok -> green", () => {
    expect(statusToDotColor("ok")).toBe("green");
  });
  it("maps expiring -> amber", () => {
    expect(statusToDotColor("expiring")).toBe("amber");
  });
  it("maps expired -> red", () => {
    expect(statusToDotColor("expired")).toBe("red");
  });
  it("maps missing -> red", () => {
    expect(statusToDotColor("missing")).toBe("red");
  });
  it("maps static -> green (statically-configured creds are healthy)", () => {
    expect(statusToDotColor("static")).toBe("green");
  });
  it("maps unknown/undefined -> neutral", () => {
    expect(statusToDotColor(undefined)).toBe("neutral");
    expect(statusToDotColor("weird")).toBe("neutral");
  });
});

describe("resolveProviderStatus", () => {
  it("matches byo_key + anthropic to the anthropic provider entry", () => {
    const list = providers(
      { provider: "anthropic", displayName: "Anthropic", status: "expiring" },
    );
    expect(resolveProviderStatus("byo_key", "anthropic", list)).toBe("expiring");
  });

  it("matches byo_key + openai to the openai provider entry by prefix", () => {
    const list = providers(
      { provider: "openai-responses", displayName: "OpenAI", status: "missing" },
    );
    expect(resolveProviderStatus("byo_key", "openai", list)).toBe("missing");
  });

  it("matches chatgpt_oauth to the openai-codex provider entry", () => {
    const list = providers(
      { provider: "openai-codex", displayName: "ChatGPT", status: "ok" },
    );
    expect(resolveProviderStatus("chatgpt_oauth", null, list)).toBe("ok");
  });

  it("matches managed inference to the custom Hermes provider", () => {
    const list = providers({ provider: "custom", displayName: "Lightbulb", status: "static" });
    expect(resolveProviderStatus("openrouter_managed", null, list)).toBe("static");
  });

  it("matches chatgpt_oauth to openai-codex even when a byo openai entry is present (exact)", () => {
    const list = providers(
      { provider: "openai-codex", displayName: "ChatGPT", status: "ok" },
      { provider: "openai", displayName: "OpenAI", status: "expired" },
    );
    expect(resolveProviderStatus("chatgpt_oauth", null, list)).toBe("ok");
  });

  it("byo openai prefers the exact openai entry over openai-codex (ChatGPT-OAuth's id)", () => {
    // openai-codex is ChatGPT OAuth's DISTINCT provider id. A BYO-OpenAI user's
    // dot must read its own `openai` key status, never the OAuth entry. Exact
    // match must win even when openai-codex is listed first.
    const list = providers(
      { provider: "openai-codex", displayName: "ChatGPT", status: "ok" },
      { provider: "openai", displayName: "OpenAI", status: "expired" },
    );
    expect(resolveProviderStatus("byo_key", "openai", list)).toBe("expired");
  });

  it("byo openai does NOT match openai-codex when only the OAuth entry exists", () => {
    // openai-codex belongs to chatgpt_oauth; the generic `openai` prefix must
    // not swallow it, so a BYO-OpenAI user with only an OAuth entry resolves to
    // undefined (their own key status is genuinely unknown).
    const list = providers(
      { provider: "openai-codex", displayName: "ChatGPT", status: "ok" },
    );
    expect(resolveProviderStatus("byo_key", "openai", list)).toBeUndefined();
  });

  it("byo openai still matches a legit openai prefix variant (openai-responses)", () => {
    const list = providers(
      { provider: "openai-responses", displayName: "OpenAI", status: "missing" },
    );
    expect(resolveProviderStatus("byo_key", "openai", list)).toBe("missing");
  });

  it("returns undefined when no provider entry matches", () => {
    const list = providers(
      { provider: "anthropic", displayName: "Anthropic", status: "ok" },
    );
    expect(resolveProviderStatus("chatgpt_oauth", null, list)).toBeUndefined();
  });

  it("returns undefined when the provider list is empty or absent", () => {
    expect(resolveProviderStatus("chatgpt_oauth", null, [])).toBeUndefined();
    expect(resolveProviderStatus("chatgpt_oauth", null, undefined)).toBeUndefined();
  });

  it("returns undefined when provider_choice is null", () => {
    const list = providers({ provider: "anthropic", displayName: "Anthropic", status: "ok" });
    expect(resolveProviderStatus(null, null, list)).toBeUndefined();
  });
});
