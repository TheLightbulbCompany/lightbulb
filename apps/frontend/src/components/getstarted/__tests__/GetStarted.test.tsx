import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GetStarted } from "../GetStarted";

vi.mock("../useGetStarted", () => ({
  useGetStarted: () => ({
    isOpen: true, minimized: false, dismissed: false, tab: "do", activeArticle: null,
    open: vi.fn(), minimize: vi.fn(), close: vi.fn(), dismiss: vi.fn(), setTab: vi.fn(),
    openArticle: vi.fn(), backToLearn: vi.fn(),
    completed: { meet: false, chat: false, task: false, hire: false, channel: false },
    progress: { done: 0, total: 5 }, seen: true,
  }),
}));

describe("GetStarted", () => {
  it("runs the matching action on a checklist CTA", async () => {
    const hire = vi.fn();
    render(<GetStarted derived={{}} actions={{ chat: vi.fn(), task: vi.fn(), hire, channel: vi.fn() }} />);
    await userEvent.click(screen.getByRole("button", { name: /browse marketplace/i }));
    expect(hire).toHaveBeenCalled();
  });
});
