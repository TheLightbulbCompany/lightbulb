// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import * as React from "react";
import { useScopedHotkey } from "../useScopedHotkey";

const isSaveHotkey = (e: KeyboardEvent) => (e.metaKey || e.ctrlKey) && e.key === "s";

function pressCmdS() {
  fireEvent.keyDown(window, { key: "s", metaKey: true });
}

function Scoped({ handler, enabled = true }: { handler: () => void; enabled?: boolean }) {
  const ref = React.useRef<HTMLDivElement | null>(null);
  useScopedHotkey(ref, isSaveHotkey, handler, enabled);
  return (
    <div ref={ref} data-testid="scope">
      <input data-testid="inside-input" />
    </div>
  );
}

describe("useScopedHotkey", () => {
  it("fires the handler when focus is inside the scoped ref", () => {
    const handler = vi.fn();
    const { getByTestId } = render(<Scoped handler={handler} />);
    (getByTestId("inside-input") as HTMLInputElement).focus();
    pressCmdS();
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("does NOT fire when focus is outside the scoped ref", () => {
    const handler = vi.fn();
    render(<Scoped handler={handler} />);
    const outside = document.createElement("input");
    document.body.appendChild(outside);
    outside.focus();
    pressCmdS();
    expect(handler).not.toHaveBeenCalled();
    outside.remove();
  });

  it("does NOT fire when disabled even if focus is inside", () => {
    const handler = vi.fn();
    const { getByTestId } = render(<Scoped handler={handler} enabled={false} />);
    (getByTestId("inside-input") as HTMLInputElement).focus();
    pressCmdS();
    expect(handler).not.toHaveBeenCalled();
  });

  it("two instances, one keystroke — only the focus-owning instance fires", () => {
    const handlerA = vi.fn();
    const handlerB = vi.fn();

    function TwoScopes() {
      const refA = React.useRef<HTMLDivElement | null>(null);
      const refB = React.useRef<HTMLDivElement | null>(null);
      useScopedHotkey(refA, isSaveHotkey, handlerA, true);
      useScopedHotkey(refB, isSaveHotkey, handlerB, true);
      return (
        <div>
          <div ref={refA} data-testid="scope-a">
            <input data-testid="input-a" />
          </div>
          <div ref={refB} data-testid="scope-b">
            <input data-testid="input-b" />
          </div>
        </div>
      );
    }

    const { getByTestId } = render(<TwoScopes />);
    (getByTestId("input-b") as HTMLInputElement).focus();
    pressCmdS();
    expect(handlerA).not.toHaveBeenCalled();
    expect(handlerB).toHaveBeenCalledTimes(1);
  });
});
