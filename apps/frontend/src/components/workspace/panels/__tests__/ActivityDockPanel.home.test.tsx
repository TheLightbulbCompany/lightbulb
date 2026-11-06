// Activity (super-agent): mentions as one line. A login ask is not here: it sits in Needs you, in the one count.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { loginAsk, maya, message } from "@/components/activity/__tests__/fixtures";

const opened = vi.hoisted(() => vi.fn());
vi.mock("../../workspaceController", () => ({ openPanel: vi.fn(), openTaskPane: vi.fn() }));
vi.mock("@/hooks/useActivity", () => ({ useActivity: vi.fn() }));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));

import { HomeForYou } from "../ActivityDockPanel";

describe("HomeForYou", () => {
  it("one Mentions line (count, who, the newest) that opens it; no login ask", async () => {
    window.addEventListener("openRoom", (event) => opened((event as CustomEvent).detail), { once: true });
    render(<HomeForYou items={[message("m2"), message("m1", maya), loginAsk]} />);
    const line = screen.getByRole("button", { name: /^Mentions · 2/ });
    expect(line).toHaveTextContent("Maya: message m2");
    await userEvent.click(line);
    expect(opened).toHaveBeenCalledWith({ roomId: "room-1", threadId: "m2" });
    expect(screen.queryByRole("button", { name: "Add login" })).toBeNull();
  });

  it("renders nothing when nothing is left over", () => {
    const { container } = render(<HomeForYou items={[message("m1", maya, true), loginAsk]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
