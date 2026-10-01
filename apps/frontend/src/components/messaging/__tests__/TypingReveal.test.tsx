import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TypingReveal } from "../TypingReveal";

const text = "Welcome to Acme. I'm your CEO. ".repeat(4).trim();

describe("TypingReveal", () => {
  beforeEach(() => { vi.useFakeTimers(); window.sessionStorage.clear(); });
  afterEach(() => { vi.useRealTimers(); });

  it("reveals the text over time, then remembers it played", () => {
    const { container } = render(<TypingReveal text={text} playKey="room-1">{(shown) => <p>{shown}</p>}</TypingReveal>);
    expect(container.textContent!.length).toBeLessThan(text.length);
    act(() => { vi.advanceTimersByTime(5000); });
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(window.sessionStorage.getItem("lightbulb.reveal.room-1")).toBe("1");
  });

  it("shows everything at once when it already played", () => {
    window.sessionStorage.setItem("lightbulb.reveal.room-1", "1");
    render(<TypingReveal text={text} playKey="room-1">{(shown) => <p>{shown}</p>}</TypingReveal>);
    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it("shows everything at once under reduced motion", () => {
    const original = window.matchMedia;
    window.matchMedia = ((q: string) => ({ matches: q.includes("reduce"), media: q, addEventListener() {}, removeEventListener() {} })) as never;
    try {
      render(<TypingReveal text={text} playKey="room-2">{(shown) => <p>{shown}</p>}</TypingReveal>);
      expect(screen.getByText(text)).toBeInTheDocument();
    } finally { window.matchMedia = original; }
  });
});
