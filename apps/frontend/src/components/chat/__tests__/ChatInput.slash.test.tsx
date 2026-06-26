// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ChatInput } from "@/components/chat/ChatInput";

// ChatInput now derives its command list from useSlashCommands, which reaches
// the gateway via useGatewayRpc -> useGateway. Mock the hook so the component
// renders without a GatewayProvider and the menu has a deterministic source.
vi.mock("@/hooks/useSlashCommands", () => ({
  useSlashCommands: (): {
    commands: {
      name: string;
      description: string;
      category: string;
      source: "native" | "skill" | "plugin";
      acceptsArgs: boolean;
    }[];
    isLoading: boolean;
  } => ({
    commands: [
      { name: "help", description: "List", category: "tools", source: "native", acceptsArgs: false },
      { name: "compact", description: "Compact session", category: "session", source: "native", acceptsArgs: true },
    ],
    isLoading: false,
  }),
}));

describe("ChatInput slash menu", () => {
  it("opens the menu when the input starts with '/'", () => {
    render(<ChatInput onSend={vi.fn()} agentId="a1" />);
    const ta = screen.getByRole("textbox");
    fireEvent.change(ta, { target: { value: "/" } });
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    expect(screen.getByText("/help")).toBeInTheDocument();
  });

  it("does not open the menu for normal text", () => {
    render(<ChatInput onSend={vi.fn()} agentId="a1" />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "hello" } });
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("Enter on an acceptsArgs command sends it immediately (no refill)", () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} agentId="a1" />);
    const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
    // /compact is acceptsArgs:true — the menu stays open (no arg typed yet) with
    // the compact row highlighted.
    fireEvent.change(ta, { target: { value: "/compact" } });
    expect(screen.getByRole("option", { selected: true })).toHaveTextContent("/compact");
    fireEvent.keyDown(ta, { key: "Enter" });
    // Enter commits the command rather than refilling "/compact ".
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith("/compact");
    expect(ta.value).toBe("");
  });

  it("Tab on an acceptsArgs command fills with a trailing space (does not send)", () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} agentId="a1" />);
    const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "/compact" } });
    fireEvent.keyDown(ta, { key: "Tab" });
    expect(onSend).not.toHaveBeenCalled();
    expect(ta.value).toBe("/compact ");
  });
});
