// @vitest-environment jsdom
// jest-dom is normally supplied by tests/setup.ts (the default vitest config).
// Importing it here as well makes this file runnable under the minimal
// vitest.config.unit.ts too; the import is idempotent.
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AskAI } from "../AskAI";
import { ASK_AI_PROMPT, ASK_AI_PROVIDERS, askAiUrl } from "../askAiPrompt";

const writeText = vi.fn(() => Promise.resolve());

beforeEach(() => {
  writeText.mockClear();
  writeText.mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
    writable: true,
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("askAiPrompt", () => {
  it("encodes the prompt into each provider's q param", () => {
    for (const p of ASK_AI_PROVIDERS) {
      const url = new URL(askAiUrl(p));
      expect(url.searchParams.get("q")).toBe(ASK_AI_PROMPT);
    }
  });

  it("names the product and the site so the AI has something to fetch", () => {
    expect(ASK_AI_PROMPT).toContain("thelightbulb.company");
    expect(ASK_AI_PROMPT).toContain("Lightbulb");
  });

  it("keeps the base URL's origin and path intact", () => {
    const claude = ASK_AI_PROVIDERS.find((p) => p.id === "claude")!;
    const url = new URL(askAiUrl(claude));
    expect(url.origin).toBe("https://claude.ai");
    expect(url.pathname).toBe("/new");
  });
});

describe("AskAI", () => {
  it("renders the heading and a tile per provider", () => {
    render(<AskAI />);
    expect(screen.getByRole("heading", { name: "Still not sure?" })).toBeInTheDocument();
    for (const p of ASK_AI_PROVIDERS) {
      expect(screen.getByRole("link", { name: new RegExp(p.label) })).toBeInTheDocument();
    }
  });

  it("points each tile at the provider with the prompt prefilled", () => {
    render(<AskAI />);
    for (const p of ASK_AI_PROVIDERS) {
      const link = screen.getByRole("link", { name: new RegExp(p.label) });
      expect(link).toHaveAttribute("href", askAiUrl(p));
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
    }
  });

  // The clipboard write is the fallback for a provider that silently stops
  // honouring ?q= — not a nicety. See askAiPrompt.ts.
  it("copies the prompt when a provider tile is clicked", () => {
    render(<AskAI />);
    fireEvent.click(screen.getByRole("link", { name: /Ask ChatGPT/ }));
    expect(writeText).toHaveBeenCalledWith(ASK_AI_PROMPT);
  });

  it("copies and confirms on the copy-prompt button", async () => {
    render(<AskAI />);
    fireEvent.click(screen.getByRole("button", { name: /Copy prompt/ }));
    expect(writeText).toHaveBeenCalledWith(ASK_AI_PROMPT);
    await waitFor(() => expect(screen.getByText("Copied")).toBeInTheDocument());
  });

  it("does not claim success when the clipboard is blocked", async () => {
    writeText.mockRejectedValue(new Error("denied"));
    render(<AskAI />);
    fireEvent.click(screen.getByRole("button", { name: /Copy prompt/ }));
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    expect(screen.queryByText("Copied")).not.toBeInTheDocument();
  });

  // Regression: `navigator.clipboard?.writeText()` short-circuits to undefined
  // when the API is absent (insecure context, webview), and `await undefined`
  // does NOT throw — so the old code reported a copy that never happened.
  it("does not claim success when the clipboard API is absent entirely", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
      writable: true,
    });
    render(<AskAI />);
    fireEvent.click(screen.getByRole("button", { name: /Copy prompt/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Copy prompt/ })).toBeInTheDocument());
    expect(screen.queryByText("Copied")).not.toBeInTheDocument();
  });

  it("still navigates when the clipboard API is absent", () => {
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
      writable: true,
    });
    render(<AskAI />);
    // Must not throw — the copy is a side-effect, never a gate on the link.
    expect(() => fireEvent.click(screen.getByRole("link", { name: /Ask Claude/ }))).not.toThrow();
  });

  it("offers a human escape hatch", () => {
    render(<AskAI />);
    expect(screen.getByRole("link", { name: "Talk to a human" })).toHaveAttribute("href", "/support");
  });
});
