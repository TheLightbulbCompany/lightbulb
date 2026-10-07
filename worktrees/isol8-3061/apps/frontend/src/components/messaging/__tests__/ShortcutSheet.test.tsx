import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ShortcutSheet } from "../ShortcutSheet";

afterEach(cleanup);

it("lists the workspace keyboard map", () => {
  render(<ShortcutSheet open onOpenChange={vi.fn()} />);
  expect(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
  for (const chord of ["⌘K", "⌥↑", "⌥↓", "⌥⇧↑", "⌥⇧↓", "Esc", "⌘/", "⌘B", "⌘I", "⌘⇧X", "⌘⇧C", "Enter", "Shift+Enter", "↑"]) {
    expect(screen.getByText(chord)).toBeInTheDocument();
  }
});

it("renders nothing while closed", () => {
  render(<ShortcutSheet open={false} onOpenChange={vi.fn()} />);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
