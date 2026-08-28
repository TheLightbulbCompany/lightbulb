import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

vi.mock("next/dynamic", () => ({
  default: () => {
    const Stub = () => <div data-testid="three-scene" />;
    return Stub;
  },
}));

import { HeroLamp } from "../index";
import { getTheme, setTheme } from "../lampStore";
import * as webgl from "../webgl";

function mockReducedMotion(reduce: boolean) {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: reduce && q.includes("prefers-reduced-motion"),
    media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

describe("HeroLamp chooses scene vs fallback", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setTheme("dark");
    vi.spyOn(webgl, "hasWebGL").mockReturnValue(true);
    mockReducedMotion(false);
  });
  afterEach(() => vi.restoreAllMocks());

  it("renders the three.js scene when WebGL is available and motion is allowed", () => {
    const { unmount } = render(<HeroLamp />);
    expect(screen.getByTestId("three-scene")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /light/i, hidden: false })).toBeInTheDocument();
    expect(document.documentElement.getAttribute("data-lb-theme")).toBe("dark");
    // the contract is home-only: a soft nav off "/" must not carry it, and the
    // cleanup runs in the mutation phase so the next route never paints dark
    unmount();
    expect(document.documentElement.getAttribute("data-lb-theme")).toBeNull();
    expect(document.documentElement.hasAttribute("data-lb-theming")).toBe(false);
  });

  it("falls back to the SVG lamp under prefers-reduced-motion", () => {
    mockReducedMotion(true);
    render(<HeroLamp />);
    expect(screen.queryByTestId("three-scene")).not.toBeInTheDocument();
    expect(document.querySelector(".ld-lamp-svg")).not.toBeNull();
    // the SVG carries its own control — the scene's button would be a second,
    // inert "Turn on the light" in the a11y tree
    expect(document.querySelector(".ld-lamp-switch")).toBeNull();
  });

  it("falls back to the SVG lamp without WebGL", () => {
    vi.spyOn(webgl, "hasWebGL").mockReturnValue(false);
    render(<HeroLamp />);
    expect(screen.queryByTestId("three-scene")).not.toBeInTheDocument();
    expect(document.querySelector(".ld-lamp-svg")).not.toBeNull();
  });

  it("the accessible switch over the glass strikes the lamp", () => {
    render(<HeroLamp />);
    fireEvent.click(screen.getByRole("button", { name: "Turn on the light" }));
    expect(getTheme()).toBe("light");
    expect(screen.getByRole("button", { name: "Turn off the light" })).toBeInTheDocument();
  });
});
