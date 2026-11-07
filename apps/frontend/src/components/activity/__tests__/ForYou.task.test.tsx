import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ActivityItem } from "@lightbulb/shared";
import { ForYouAsks, rowTarget } from "../ForYou";
import { approval } from "./fixtures";

vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));

/** What `activity.py render_task_question` emits for an open question the viewer can answer. */
const taskQuestion: ActivityItem = {
  ...approval, id: "taskq:t1:q1", kind: "task_question", subtype: "approval",
  actor: { kind: "agent", id: "", name: "Lightbulb" },
  summary: { title: "Allow Lightbulb · Engineering to provision a database?", requester: { kind: "agent", id: "", name: "Lightbulb" }, reason: null, preview: null,
    links: [{ kind: "task", id: "t1", label: "Workshop signup app" }] },
  actions: [{ id: "open", label: "Open thread", style: "primary", confirm: null, input: null, navigate: { kind: "task", id: "t1", roomId: null } }],
};

describe("For you: task questions", () => {
  it("rowTarget points at the thread, not a room", () => {
    expect(rowTarget(taskQuestion)).toEqual({ kind: "task", id: "t1", roomId: null });
  });

  it("the row opens the thread pane through onNavigate", async () => {
    const props = { items: [taskQuestion], onRunAction: vi.fn().mockResolvedValue(undefined), onNavigate: vi.fn() };
    render(<ForYouAsks {...props} />);
    await userEvent.click(screen.getByRole("button", { name: "Open thread" }));
    expect(props.onNavigate).toHaveBeenCalledWith({ kind: "task", id: "t1", roomId: null });
  });
});
