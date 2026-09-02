import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { buildModelRef, type ModelEntry } from "@/lib/models";

interface RpcResult<T> {
  data: T | undefined;
  error: Error | undefined;
  isLoading: boolean;
  mutate: () => Promise<T | undefined>;
}

const mockRpc = vi.fn();
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (method: string | null, params?: Record<string, unknown>) =>
    mockRpc(method, params) as RpcResult<unknown>,
}));

import { useAgentModels } from "@/hooks/useAgentModels";

const rpc = <T,>(over: Partial<RpcResult<T>>): RpcResult<T> => ({
  data: undefined,
  error: undefined,
  isLoading: false,
  mutate: async () => undefined,
  ...over,
});

const CONFIG = {
  config: {
    agents: {
      defaults: { model: "amazon-bedrock/default-x" },
      list: [{ id: "agent_a", model: "openai/gpt-5" }],
    },
  },
};

beforeEach(() => mockRpc.mockReset());

describe("useAgentModels", () => {
  it("requests the configured view for the agent, matching upstream's picker", () => {
    // The gateway resolves `default` against the static plugin manifest and
    // races full discovery at 750ms, serving the stale static list on timeout
    // (models-list-result.ts). `configured` waits for account-scoped
    // discovery and keeps the agent's pinned model visible even when the
    // account's live list omits it -- the request shape the upstream Control
    // UI sends (ui/src/lib/model-catalog-store.ts).
    mockRpc.mockImplementation((method: string) =>
      method === "models.list" ? rpc({ data: { models: [] } }) : rpc({ data: CONFIG }),
    );
    renderHook(() => useAgentModels("agent_a"));
    expect(mockRpc).toHaveBeenCalledWith("models.list", { view: "configured", agentId: "agent_a" });
  });

  it("returns models.list entries when the catalog resolves", () => {
    const models: ModelEntry[] = [
      { id: "gpt-5", provider: "openai", name: "GPT-5", available: true },
    ];
    mockRpc.mockImplementation((method: string) =>
      method === "models.list" ? rpc({ data: { models } }) : rpc({ data: CONFIG }),
    );
    const { result } = renderHook(() => useAgentModels("agent_a"));
    expect(result.current.usingFallback).toBe(false);
    expect(result.current.models).toHaveLength(1);
    expect(result.current.models[0].id).toBe("gpt-5");
    expect(mockRpc).toHaveBeenCalledWith("models.list", { view: "configured", agentId: "agent_a" });
    expect(mockRpc).toHaveBeenCalledWith("config.get", undefined);
  });

  it("falls back to the agent default model on catalog error", () => {
    mockRpc.mockImplementation((method: string) =>
      method === "models.list"
        ? rpc({ error: new Error("timeout") })
        : rpc({ data: CONFIG }),
    );
    const { result } = renderHook(() => useAgentModels("agent_a"));
    expect(result.current.usingFallback).toBe(true);
    expect(result.current.defaultModel).toBe("openai/gpt-5");
    // The fallback entry's id is the BARE id (not the full ref) + the provider,
    // so buildModelRef(provider, id) round-trips to the original ref instead of
    // double-prefixing (openai/openai/gpt-5).
    const entry = result.current.models[0];
    expect(entry.id).toBe("gpt-5");
    expect(entry.provider).toBe("openai");
    expect(buildModelRef(entry.provider, entry.id)).toBe("openai/gpt-5");
  });

  it("keeps internal slashes in a Bedrock ARN fallback so buildModelRef round-trips", () => {
    const arnRef =
      "amazon-bedrock/arn:aws:bedrock:us-east-1::application-inference-profile/abc123";
    mockRpc.mockImplementation((method: string) =>
      method === "models.list"
        ? rpc({ error: new Error("timeout") })
        : rpc({ data: { config: { agents: { defaults: { model: arnRef } } } } }),
    );
    const { result } = renderHook(() => useAgentModels("agent_z"));
    const entry = result.current.models[0];
    expect(entry.provider).toBe("amazon-bedrock");
    // Everything after the FIRST slash is preserved — the ARN keeps its own `/`.
    expect(entry.id).toBe(
      "arn:aws:bedrock:us-east-1::application-inference-profile/abc123",
    );
    expect(buildModelRef(entry.provider, entry.id)).toBe(arnRef);
  });

  it("falls back to defaults.model when the agent has no per-agent model and list is empty", () => {
    mockRpc.mockImplementation((method: string) =>
      method === "models.list"
        ? rpc({ data: { models: [] } })
        : rpc({ data: { config: { agents: { defaults: { model: "amazon-bedrock/default-x" } } } } }),
    );
    const { result } = renderHook(() => useAgentModels("agent_z"));
    expect(result.current.usingFallback).toBe(true);
    expect(result.current.defaultModel).toBe("amazon-bedrock/default-x");
  });
});
