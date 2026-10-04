// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { HistoryButtons, historyAvailability } from "../HistoryButtons";

type Win = Window & { navigation?: unknown };

function fakeNavigation(paths: string[], index: number) {
  (window as Win).navigation = {
    entries: () => paths.map((path) => ({ url: `${window.location.origin}${path}` })),
    currentEntry: { index },
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
}

beforeEach(() => window.history.replaceState(null, "", "/chat"));
afterEach(() => {
  delete (window as Win).navigation;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("historyAvailability", () => {
  it("stays enabled where the Navigation API is missing", () => {
    expect(historyAvailability()).toEqual({ back: true, forward: true });
  });

  it("only steps to entries inside the app", () => {
    fakeNavigation(["/sign-in", "/chat", "/chat?panel=teams-issues"], 1);
    expect(historyAvailability()).toEqual({ back: false, forward: true });
    fakeNavigation(["/chat", "/chat?panel=chat&room=r1"], 1);
    expect(historyAvailability()).toEqual({ back: true, forward: false });
  });
});

describe("HistoryButtons", () => {
  it("steps back and forward", () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});
    const forward = vi.spyOn(window.history, "forward").mockImplementation(() => {});
    render(<HistoryButtons />);
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(screen.getByRole("button", { name: "Forward" }));
    expect(back).toHaveBeenCalledTimes(1);
    expect(forward).toHaveBeenCalledTimes(1);
  });

  it("maps ⌘[ and ⌘] by key position, so layouts that need Alt for [ still work", () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});
    const forward = vi.spyOn(window.history, "forward").mockImplementation(() => {});
    render(<HistoryButtons />);
    fireEvent.keyDown(window, { code: "BracketLeft", key: "[", metaKey: true });
    fireEvent.keyDown(window, { code: "BracketRight", key: "]", ctrlKey: true });
    fireEvent.keyDown(window, { code: "BracketLeft", key: "[", metaKey: true, altKey: true });
    expect(back).toHaveBeenCalledTimes(2);
    expect(forward).toHaveBeenCalledTimes(1);
  });

  it("ignores ⌘[ while a dialog or menu is open", () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});
    render(<HistoryButtons />);
    for (const role of ["dialog", "menu"]) {
      const overlay = document.createElement("div");
      overlay.setAttribute("role", role);
      document.body.appendChild(overlay);
      fireEvent.keyDown(window, { code: "BracketLeft", key: "[", metaKey: true });
      overlay.remove();
    }
    expect(back).not.toHaveBeenCalled();
  });
});
