import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, applySubagentEvent, EMPTY_ROOM_SUBAGENTS } from "@lightbulb/shared";
import { RoomSubagentStrip } from "../RoomSubagentStrip";

const mk = (n: number) => Array.from({ length: n }).reduce<typeof EMPTY_ROOM_SUBAGENTS>((s, _, i) =>
  applySubagentEvent(s, "a1", "subagent.tool", { subagent_id: `sa-${i}`, goal: `Track ${i}`, tool_name: "web_search", tool_preview: "q" }, Date.now()),
  EMPTY_ROOM_SUBAGENTS);
const props = (state = mk(3)) => ({
  state, roomId: "r1", agentName: () => "Forge", onVisible: vi.fn(),
  onSteer: vi.fn().mockResolvedValue({ state: "queued" }), onStop: vi.fn().mockResolvedValue({ state: "stopping" }),
  client: { subagentTail: vi.fn().mockResolvedValue({ subagentId: "sa-0", available: true, text: "line 1\nline 2", truncated: false }) } as never,
});

describe("RoomSubagentStrip", () => {
  it("renders nothing without live subagents", () => {
    const { container } = render(<RoomSubagentStrip {...props(EMPTY_ROOM_SUBAGENTS)} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the count, three rows and +N more", () => {
    render(<RoomSubagentStrip {...props(mk(5))} />);
    expect(screen.getByRole("region", { name: "Running subagents" })).toBeInTheDocument();
    expect(screen.getByText("Subagents (5)")).toBeInTheDocument();
    expect(screen.getAllByText(/Track \d/)).toHaveLength(3);
    expect(screen.getByRole("button", { name: "+2 more" })).toBeInTheDocument();
  });

  it("a row opens steer, stop and the transcript; steer says queued", async () => {
    const p = props();
    render(<RoomSubagentStrip {...p} />);
    fireEvent.click(screen.getByRole("button", { name: /Track 0/ }));
    await waitFor(() => expect(screen.getByText(/line 2/)).toBeInTheDocument());
    fireEvent.change(screen.getByRole("textbox", { name: "Steer Track 0" }), { target: { value: "focus on Library" } });
    fireEvent.click(screen.getByRole("button", { name: "Steer" }));
    await waitFor(() => expect(screen.getByText("Steer queued")).toBeInTheDocument());
    expect(p.onSteer).toHaveBeenCalledWith("a1", "sa-0", "focus on Library");
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(p.onStop).toHaveBeenCalledWith("a1", "sa-0");
  });

  it("a finished subagent's steer shows the 404 copy", async () => {
    const p = { ...props(), onSteer: vi.fn().mockRejectedValue(new ApiError(404, { code: "SUBAGENT_NOT_FOUND" })) };
    render(<RoomSubagentStrip {...p} />);
    fireEvent.click(screen.getByRole("button", { name: /Track 0/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "Steer Track 0" }), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Steer" }));
    await waitFor(() => expect(screen.getByText("That subagent has already finished.")).toBeInTheDocument());
  });
});
