import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { SessionToolIcon } from "../SessionToolIcon";

it("names each tool's logo for screen readers and falls back to a terminal glyph for an unknown tool", () => {
  render(<><SessionToolIcon provider="claude" /><SessionToolIcon provider="codex" /><SessionToolIcon provider="gemini" /></>);
  expect(screen.getByRole("img", { name: "Claude" })).toBeInTheDocument();
  expect(screen.getByRole("img", { name: "Codex" })).toHaveAttribute("fill", "currentColor");
  expect(screen.getByRole("img", { name: "gemini" })).toHaveClass("lucide-terminal");
});

it("draws Cursor and Grok CLI in ink, not as the unknown terminal", () => {
  render(<><SessionToolIcon provider="cursor" /><SessionToolIcon provider="grok" /></>);
  for (const name of ["Cursor", "Grok"]) {
    const icon = screen.getByRole("img", { name });
    expect(icon).toHaveAttribute("stroke", "currentColor");
    expect(icon).not.toHaveClass("lucide-terminal");
  }
});
