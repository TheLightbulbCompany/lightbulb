import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { UseGetStarted } from "../useGetStarted";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

const gsRef: { current: UseGetStarted } = { current: baseGs() };

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

vi.mock("../useGetStarted", () => ({ useGetStarted: () => gsRef.current }));

import { GetStarted } from "../GetStarted";

function makeActions() {
  return { chat: vi.fn(), task: vi.fn(), hire: vi.fn(), channel: vi.fn() };
}

describe("GetStarted", () => {
  beforeEach(() => {
    gsRef.current = baseGs();
  });

  it("runs the matching action on a checklist CTA", async () => {
    const actions = makeActions();
    render(<GetStarted derived={{}} actions={actions} />);
    await userEvent.click(screen.getByRole("button", { name: /browse marketplace/i }));
    expect(actions.hire).toHaveBeenCalled();
  });

  it("renders the launcher while steps are incomplete", () => {
    gsRef.current = { ...baseGs(), isOpen: false };
    const { container } = render(<GetStarted derived={{}} actions={makeActions()} />);
    expect(container).not.toBeEmptyDOMElement();
  });

  it("renders nothing once dismissed", () => {
    gsRef.current = { ...baseGs(), dismissed: true };
    const { container } = render(<GetStarted derived={{}} actions={makeActions()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when completedFired was persisted (reload after completion)", () => {
    gsRef.current = { ...baseGs(), completedFired: true };
    const { container } = render(<GetStarted derived={{}} actions={makeActions()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing the moment live progress hits total", () => {
    gsRef.current = {
      ...baseGs(),
      completed: { meet: true, chat: true, task: true, hire: true, channel: true },
      progress: { done: 5, total: 5 },
    };
    const { container } = render(<GetStarted derived={{}} actions={makeActions()} />);
    expect(container).toBeEmptyDOMElement();
  });
});
