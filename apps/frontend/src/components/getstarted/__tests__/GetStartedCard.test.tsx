import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GetStartedCard } from "../GetStartedCard";

function gsStub(over = {}) {
  return { isOpen: true, minimized: false, tab: "do", activeArticle: null,
    open: vi.fn(), minimize: vi.fn(), close: vi.fn(), dismiss: vi.fn(), setTab: vi.fn(),
    openArticle: vi.fn(), backToLearn: vi.fn(),
    completed: { meet: false, chat: false, task: false, hire: false, channel: false },
    progress: { done: 0, total: 5 }, dismissed: false, seen: true, ...over } as unknown as Parameters<typeof GetStartedCard>[0]["gs"];
}

describe("GetStartedCard", () => {
  it("switches to the learn tab", async () => {
    const setTab = vi.fn();
    render(<GetStartedCard gs={gsStub({ setTab })} onAction={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /learn the basics/i }));
    expect(setTab).toHaveBeenCalledWith("learn");
  });
  it("renders an opened article", () => {
    render(<GetStartedCard gs={gsStub({ tab: "learn", activeArticle: "meet-friday-atlas" })} onAction={vi.fn()} />);
    expect(screen.getByRole("heading", { name: /meet your starter team/i })).toBeInTheDocument();
  });
});
