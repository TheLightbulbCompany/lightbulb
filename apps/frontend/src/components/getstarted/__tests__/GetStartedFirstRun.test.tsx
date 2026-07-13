import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { UseGetStarted } from "../useGetStarted";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({
    user: { unsafeMetadata: { firstRun: { briefSubmitted: true } }, update: vi.fn() },
    isLoaded: true,
  }),
}));

// Progress is deliberately incomplete/not-dismissed here — the ONLY thing
// that should suppress rendering is the first-run brief-submitted check.
// If that check were missing, this stub would make the launcher render.
function baseGs(): UseGetStarted {
  return {
    isOpen: true, minimized: false, tab: "do", activeArticle: null,
    open: vi.fn(), minimize: vi.fn(), close: vi.fn(), dismiss: vi.fn(), setTab: vi.fn(),
    openArticle: vi.fn(), backToLearn: vi.fn(),
    completed: { meet: false, chat: false, task: false, hire: false, channel: false },
    progress: { done: 0, total: 5 },
    dismissed: false, seen: true, completedFired: false,
  };
}

const useGetStartedSpy = vi.fn(() => baseGs());
vi.mock("../useGetStarted", () => ({ useGetStarted: () => useGetStartedSpy() }));

import { GetStarted } from "../GetStarted";

describe("GetStarted with an active first-run", () => {
  it("renders nothing when the user submitted a first-run brief", () => {
    const actions = { chat: vi.fn(), task: vi.fn(), hire: vi.fn(), channel: vi.fn() };
    const { container } = render(<GetStarted derived={{}} actions={actions} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("never mounts useGetStarted for a first-run user (no hook side effects)", () => {
    useGetStartedSpy.mockClear();
    const actions = { chat: vi.fn(), task: vi.fn(), hire: vi.fn(), channel: vi.fn() };
    render(<GetStarted derived={{}} actions={actions} />);
    // The first-run gate returns before rendering the guide, so the hook —
    // and its auto-open / capture / emitStep side effects — never runs.
    expect(useGetStartedSpy).not.toHaveBeenCalled();
  });
});
