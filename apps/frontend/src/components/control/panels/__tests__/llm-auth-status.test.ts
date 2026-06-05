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
  it("matches bedrock_claude to the bedrock provider entry", () => {
    const list = providers(
      { provider: "bedrock", displayName: "Bedrock", status: "ok" },
      { provider: "anthropic", displayName: "Anthropic", status: "expired" },
    );
    expect(resolveProviderStatus("bedrock_claude", null, list)).toBe("ok");
  });

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

  it("returns undefined when no provider entry matches", () => {
    const list = providers(
      { provider: "anthropic", displayName: "Anthropic", status: "ok" },
    );
    expect(resolveProviderStatus("chatgpt_oauth", null, list)).toBeUndefined();
  });

  it("returns undefined when the provider list is empty or absent", () => {
    expect(resolveProviderStatus("bedrock_claude", null, [])).toBeUndefined();
    expect(resolveProviderStatus("bedrock_claude", null, undefined)).toBeUndefined();
  });

  it("returns undefined when provider_choice is null", () => {
    const list = providers({ provider: "bedrock", displayName: "B", status: "ok" });
    expect(resolveProviderStatus(null, null, list)).toBeUndefined();
  });
});
