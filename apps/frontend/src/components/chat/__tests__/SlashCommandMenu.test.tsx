import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { SlashCommandMenu, flattenVisible } from "@/components/chat/SlashCommandMenu";
import type { SlashCommand } from "@/lib/slashCommands";

const CMDS: SlashCommand[] = [
  { name: "reset", description: "Reset session", category: "session", source: "native", acceptsArgs: true },
  { name: "model", description: "Switch model", category: "model", source: "native", acceptsArgs: true },
  { name: "deep-research", description: "Research", category: "tools", source: "skill", acceptsArgs: true },
];

describe("SlashCommandMenu", () => {
  it("renders category headers and command rows", () => {
    render(<SlashCommandMenu commands={CMDS} query="" activeIndex={0} onSelect={vi.fn()} onActiveIndexChange={vi.fn()} />);
    expect(screen.getByText("/reset")).toBeInTheDocument();
    expect(screen.getByText(/Research/)).toBeInTheDocument();
  });
  it("flattenVisible returns rows in display order for keyboard nav", () => {
    expect(flattenVisible(CMDS, "").map((c) => c.name)).toEqual(["reset", "model", "deep-research"]);
  });
  it("marks a skill-sourced row with its source", () => {
    render(<SlashCommandMenu commands={CMDS} query="" activeIndex={0} onSelect={vi.fn()} onActiveIndexChange={vi.fn()} />);
    expect(screen.getByText("Skills")).toBeInTheDocument(); // group label for source:"skill"
  });
});
