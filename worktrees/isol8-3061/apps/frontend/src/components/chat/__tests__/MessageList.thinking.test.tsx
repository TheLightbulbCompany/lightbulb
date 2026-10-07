/**
 * ThinkingBlock's label tense + chrome.
 *
 * The bug this guards: the block rendered the literal string "Thinking..."
 * forever — present tense even in scrollback for reasoning that finished an
 * hour ago. A settled thought now reads in the past ("Thought"), and only
 * while `streaming` is true does it read as live.
 *
 * No real elapsed time is on the wire for thinking (unlike tool calls'
 * startedAt/endedAt), so the settled label never carries a duration — a
 * `durationMs` prop for a future wire caller that never arrived was removed
 * as dead code (no call site ever passed it).
 */

import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ThinkingBlock } from "../MessageList";

describe("ThinkingBlock — settled tense", () => {
  it("reads present-tense while actually streaming", () => {
    render(<ThinkingBlock content="reasoning" streaming />);
    expect(screen.getByText("Thinking…")).toBeInTheDocument();
    expect(screen.queryByText(/Thought/)).not.toBeInTheDocument();
  });

  it("falls back to a tenseless label once settled", () => {
    render(<ThinkingBlock content="reasoning" />);
    expect(screen.getByText("Thought")).toBeInTheDocument();
    expect(screen.queryByText(/Thinking\.\.\./)).not.toBeInTheDocument();
  });
});

describe("ThinkingBlock — touch target", () => {
  it("carries the coarse-pointer hit-area class without a visible box on desktop", () => {
    // .chat-thinking-trigger bumps the tap target via padding+negative-margin
    // under @media (pointer: coarse) in globals.css (jsdom doesn't evaluate
    // that query, so this only pins the class wiring, not the computed box).
    render(<ThinkingBlock content="reasoning" />);
    const trigger = screen.getByRole("button");
    expect(trigger).toHaveClass("chat-thinking-trigger");
    // Fine-pointer/desktop chrome is unchanged — no boxed padding classes.
    expect(trigger.className).not.toMatch(/\bpx-3\b|\bpy-2\b/);
  });
});

describe("ThinkingBlock — mount/collapse behaviour preserved", () => {
  it("mounts expanded while streaming and auto-collapses when the stream ends", () => {
    const { rerender } = render(<ThinkingBlock content="reasoning" streaming />);
    expect(screen.getByText("reasoning")).toBeVisible();
    rerender(<ThinkingBlock content="reasoning" streaming={false} />);
    expect(screen.queryByText("reasoning")).not.toBeInTheDocument();
  });

  it("mounts collapsed for a settled history block", () => {
    render(<ThinkingBlock content="reasoning" />);
    expect(screen.queryByText("reasoning")).not.toBeInTheDocument();
  });

  it("an explicit user toggle survives the streaming→settled auto-collapse", () => {
    const { rerender } = render(<ThinkingBlock content="reasoning" streaming />);
    const toggle = () => fireEvent.click(screen.getByRole("button"));
    // Collapse then re-expand while still streaming — any click arms the
    // escape hatch, even one that lands back on the default (expanded) value.
    toggle();
    toggle();
    expect(screen.getByText("reasoning")).toBeVisible();
    rerender(<ThinkingBlock content="reasoning" streaming={false} />);
    // Without the escape hatch, settling would force this collapsed.
    expect(screen.getByText("reasoning")).toBeVisible();
  });
});
