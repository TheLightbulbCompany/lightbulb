import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { HangingLamp } from "../HangingLamp";
import { getTheme, setTheme } from "../HeroLamp/lampStore";

// The pendulum effect queries prefers-reduced-motion; jsdom has no matchMedia.
window.matchMedia =
  window.matchMedia ||
  ((query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }) as unknown as MediaQueryList);

describe("HangingLamp (SVG fallback)", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setTheme("dark");
  });

  it("starts dark and offers to turn the light on", () => {
    render(<HangingLamp />);
    expect(screen.getByRole("button", { name: "Turn on the light" })).toBeInTheDocument();
    expect(document.documentElement.getAttribute("data-lb-theme")).toBe("dark");
  });

  it("clicking the glass strikes the lamp and relabels the control", () => {
    render(<HangingLamp />);
    fireEvent.click(screen.getByRole("button", { name: "Turn on the light" }));
    expect(getTheme()).toBe("light");
    expect(screen.getByRole("button", { name: "Turn off the light" })).toBeInTheDocument();
  });

  it("Enter and Space on the glass also strike", () => {
    render(<HangingLamp />);
    const glass = screen.getByRole("button", { name: "Turn on the light" });
    fireEvent.keyDown(glass, { key: "Enter" });
    expect(getTheme()).toBe("light");
    fireEvent.keyDown(screen.getByRole("button", { name: "Turn off the light" }), { key: " " });
    expect(getTheme()).toBe("dark");
  });
});
