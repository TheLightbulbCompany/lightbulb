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

  it("disables Steer and Stop while a request is in flight", async () => {
    let settle: (v: unknown) => void = () => {};
    const p = { ...props(), onStop: vi.fn(() => new Promise((r) => { settle = r; })) };
    render(<RoomSubagentStrip {...p} />);
    fireEvent.click(screen.getByRole("button", { name: /Track 0/ }));
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(p.onStop).toHaveBeenCalledTimes(1);
    settle({ state: "stopping" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled());
  });

  it("keeps the pane open on a child that finished, with its final status and last transcript, and stops polling", async () => {
    const p = props(mk(1));
    const { rerender } = render(<RoomSubagentStrip {...p} />);
    fireEvent.click(screen.getByRole("button", { name: /Track 0/ }));
    await waitFor(() => expect(screen.getByText(/line 2/)).toBeInTheDocument());
    const done = applySubagentEvent(p.state, "a1", "subagent.complete", { subagent_id: "sa-0", goal: "Track 0", status: "completed" }, Date.now());
    rerender(<RoomSubagentStrip {...p} state={done} />);
    expect(screen.getByText("Finished")).toBeInTheDocument();
    expect(screen.getByText(/line 2/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
    const calls = (p.client as unknown as { subagentTail: ReturnType<typeof vi.fn> }).subagentTail.mock.calls.length;
    await new Promise((r) => setTimeout(r, 2_100));
    expect((p.client as unknown as { subagentTail: ReturnType<typeof vi.fn> }).subagentTail).toHaveBeenCalledTimes(calls);
  });

  it("shows a finished child's summary when no transcript was ever received", async () => {
    const p = props(mk(1));
    (p.client as unknown as { subagentTail: ReturnType<typeof vi.fn> }).subagentTail.mockResolvedValue({ subagentId: "sa-0", available: false, text: "", truncated: false });
    const { rerender } = render(<RoomSubagentStrip {...p} />);
    fireEvent.click(screen.getByRole("button", { name: /Track 0/ }));
    const done = applySubagentEvent(p.state, "a1", "subagent.complete", { subagent_id: "sa-0", goal: "Track 0", status: "completed", summary: "Found 3 libraries" }, Date.now());
    rerender(<RoomSubagentStrip {...p} state={done} />);
    expect(await screen.findByText("Found 3 libraries")).toBeInTheDocument();
    expect(screen.queryByText("No transcript yet.")).toBeNull();
  });
});
