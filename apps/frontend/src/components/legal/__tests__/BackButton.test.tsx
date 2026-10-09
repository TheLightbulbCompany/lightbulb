import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const back = vi.fn();
const push = vi.fn();
let path = "/terms";
vi.mock("next/navigation", () => ({ useRouter: () => ({ back, push }), usePathname: () => path }));

import { BackButton, InAppNavTracker } from "../BackButton";

beforeEach(() => { back.mockClear(); push.mockClear(); });

describe("BackButton", () => {
  it("goes to the fallback on a direct visit, then back once the reader has navigated in-app", () => {
    const view = render(<><InAppNavTracker /><BackButton fallback="/home" /></>);
    fireEvent.click(screen.getByRole("button", { name: /back/i }));
    expect(push).toHaveBeenCalledWith("/home");
    expect(back).not.toHaveBeenCalled();

    path = "/privacy";
    view.rerender(<><InAppNavTracker /><BackButton fallback="/home" /></>);
    fireEvent.click(screen.getByRole("button", { name: /back/i }));
    expect(back).toHaveBeenCalledTimes(1);
  });
});
