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

vi.mock("../useGetStarted", () => ({ useGetStarted: () => baseGs() }));

import { GetStarted } from "../GetStarted";

describe("GetStarted with an active first-run", () => {
  it("renders nothing when the user submitted a first-run brief", () => {
    const actions = { chat: vi.fn(), task: vi.fn(), hire: vi.fn(), channel: vi.fn() };
    const { container } = render(<GetStarted derived={{}} actions={actions} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
