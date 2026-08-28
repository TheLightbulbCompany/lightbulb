/**
 * CodeBlock's copy affordance.
 *
 * Three bugs this guards, all present before this change:
 *   1. `codeString` stripped the fence's trailing newline before copying, so
 *      pasted code silently lost it — even though the rendered block (which
 *      SHOULD drop that one newline, or Prism shows a trailing blank line)
 *      was correct. Render and copy need different strings; this file pins
 *      that they now are.
 *   2. The button gave no feedback at all — click, and nothing visibly
 *      happens whether the copy succeeded or the promise silently rejected.
 *   3. `navigator.clipboard` is undefined on insecure origins; calling
 *      `.writeText` unguarded threw instead of no-op'ing.
 *
 * jsdom has no `navigator.clipboard` by default (see ChatGPTOAuthStep.test.tsx),
 * so it doubles as the "insecure origin" case whenever a test doesn't stub it
 * in — that's exercised explicitly below rather than left implicit.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { CodeBlock } from "../MessageList";

const writeTextMock = vi.fn();

function stubClipboard() {
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: writeTextMock },
    configurable: true,
    writable: true,
  });
}

function removeClipboard() {
  Object.defineProperty(navigator, "clipboard", {
    value: undefined,
    configurable: true,
    writable: true,
  });
}

beforeEach(() => {
  // The global afterEach (tests/setup.ts) runs vi.restoreAllMocks() after
  // EVERY test in every file, which strips any mockResolvedValue/
  // mockImplementation set outside a test's own beforeEach/it body back to
  // a bare vi.fn() returning undefined. Re-arming it here, every test,
  // means it survives that regardless of what ran before this file's first
  // test — a one-time `vi.fn().mockResolvedValue(...)` at module scope
  // looked correct and passed test 1, then silently broke every test after
  // it once the prior test's global afterEach fired.
  writeTextMock.mockReset().mockResolvedValue(undefined);
  stubClipboard();
});

afterEach(() => {
  removeClipboard();
});

describe("CodeBlock — copy affordance", () => {
  it("copies the exact source, trailing newline included", () => {
    render(<CodeBlock className="language-python">{"print(1)\n"}</CodeBlock>);
    fireEvent.click(screen.getByRole("button", { name: /copy code/i }));
    expect(writeTextMock).toHaveBeenCalledWith("print(1)\n");
  });

  it("announces success via aria-label and icon without changing the visible label text", async () => {
    render(<CodeBlock className="language-python">{"print(1)\n"}</CodeBlock>);
    const button = screen.getByRole("button", { name: /copy code/i });

    // The "Copy" word is always in the DOM, before and after — nothing swaps
    // to a wider "Copied" string that would push the button (and, since the
    // header row is `justify-between`, its own position) around.
    expect(screen.getByText("Copy")).toBeInTheDocument();

    fireEvent.click(button);

    // findByRole polls/retries instead of assuming a fixed microtask-tick
    // count to flush the `await` inside handleCopy — the prior single
    // `await Promise.resolve()` flush was tied to the old `.then()` chain's
    // exact tick count, which is exactly the kind of assumption that broke
    // once handleCopy became `async`/`await`.
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
    expect(screen.getByText("Copy")).toBeInTheDocument();
  });

  it("reverts to the idle copy affordance after the confirmation window", async () => {
    vi.useFakeTimers();
    try {
      render(<CodeBlock className="language-python">{"print(1)\n"}</CodeBlock>);

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /copy code/i }));
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1500);
      });
      expect(screen.getByRole("button", { name: /copy code/i })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not throw when navigator.clipboard is unavailable (insecure origin)", () => {
    removeClipboard();
    render(<CodeBlock className="language-python">{"print(1)\n"}</CodeBlock>);
    const button = screen.getByRole("button", { name: /copy code/i });

    expect(() => fireEvent.click(button)).not.toThrow();
    expect(writeTextMock).not.toHaveBeenCalled();
    // No false "success" — the guard bails before anything is copied.
    expect(screen.queryByRole("button", { name: "Copied" })).not.toBeInTheDocument();
  });

  it("uses CSS tokens, never a hardcoded hex, for inline code and the fenced block's background", () => {
    const { container: fenced } = render(
      <CodeBlock className="language-python">{"print(1)\n"}</CodeBlock>,
    );
    expect(fenced.innerHTML).not.toMatch(/#f8f5f0/i);
    expect(fenced.innerHTML).toContain("var(--cream)");

    const { container: inline } = render(<CodeBlock>inline_value</CodeBlock>);
    const code = inline.querySelector("code");
    expect(code?.className).not.toMatch(/#e8e3d9/i);
    expect(code?.className).toContain("var(--cream-deeper)");
  });
});
