import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useSlashCommands } from "@/hooks/useSlashCommands";

// Mock useGatewayRpc to return a fixed commands.list payload.
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (): { data: unknown; isLoading: boolean } => ({
    data: {
      commands: [
        { name: "help", description: "List", category: "tools", source: "native", acceptsArgs: false },
        { name: "focus", description: "bind thread", category: "agents", source: "native", acceptsArgs: false },
      ],
    },
    isLoading: false,
  }),
}));

describe("useSlashCommands", () => {
  it("maps payload and drops deny-listed commands", () => {
    const { result } = renderHook(() => useSlashCommands("a1"));
    const names = result.current.commands.map((c) => c.name);
    expect(names).toContain("help");
    expect(names).not.toContain("focus"); // in HIDDEN_COMMANDS
  });
});
