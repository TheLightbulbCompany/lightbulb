import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { applySubagentEvent, EMPTY_ROOM_SUBAGENTS } from "@lightbulb/shared";
import { DelegateCard } from "../DelegateCard";
import { RoomSubagentStrip } from "../RoomSubagentStrip";

const state = [0, 1, 2, 3].reduce((s, i) => applySubagentEvent(s, "a1", "subagent.tool",
  { subagent_id: `sa-${i}`, goal: `Track ${i}`, tool_name: "web_search", tool_preview: "q" }, Date.now()), EMPTY_ROOM_SUBAGENTS);

const setup = async (over: Record<string, unknown> = {}) => {
  const onSteer = jest.fn().mockResolvedValue({ state: "queued" });
  const onStop = jest.fn().mockResolvedValue({ state: "stopping" });
  const onVisible = jest.fn();
  const client = { subagentTail: jest.fn().mockResolvedValue({ subagentId: "sa-0", available: true, text: "line 2", truncated: false }) } as never;
  await render(<RoomSubagentStrip state={state} roomId="r1" agentName={() => "Forge"} client={client} onSteer={onSteer} onStop={onStop} onVisible={onVisible} {...over} />);
  return { onSteer, onStop, onVisible };
};

it("shows three rows and +1 more; a row opens a sheet with steer, stop and transcript", async () => {
  const { onSteer, onStop } = await setup();
  expect(screen.getByText("Subagents (4)")).toBeTruthy();
  expect(screen.getByText("+1 more")).toBeTruthy();
  await fireEvent.press(screen.getByLabelText(/Track 0/));
  await waitFor(() => expect(screen.getByText(/line 2/)).toBeTruthy());
  await fireEvent.changeText(screen.getByLabelText("Steer Track 0"), "focus");
  await fireEvent.press(screen.getByLabelText("Steer"));
  await waitFor(() => expect(screen.getByText("Steer queued")).toBeTruthy());
  expect(onSteer).toHaveBeenCalledWith("a1", "sa-0", "focus");
  await fireEvent.press(screen.getByLabelText("Stop"));
  expect(onStop).toHaveBeenCalledWith("a1", "sa-0");
});

it("says a finished subagent is gone when the route answers SUBAGENT_NOT_FOUND", async () => {
  const onStop = jest.fn().mockRejectedValue({ body: { code: "SUBAGENT_NOT_FOUND" } });
  await setup({ onStop });
  await fireEvent.press(screen.getByLabelText(/Track 0/));
  await fireEvent.press(screen.getByLabelText("Stop"));
  await waitFor(() => expect(screen.getByText("That subagent has already finished.")).toBeTruthy());
});

it("disables Stop while a request is in flight", async () => {
  let release!: () => void;
  const onStop = jest.fn(() => new Promise<void>((r) => { release = r; }));
  await setup({ onStop });
  await fireEvent.press(screen.getByLabelText(/Track 0/));
  await fireEvent.press(screen.getByLabelText("Stop"));
  await fireEvent.press(screen.getByLabelText("Stop"));
  expect(onStop).toHaveBeenCalledTimes(1);
  release();
  await waitFor(() => expect(screen.getByText("Stopping…")).toBeTruthy());
});

it("reports visibility to the poller", async () => {
  const { onVisible } = await setup();
  expect(onVisible).toHaveBeenCalledWith(true);
});

it("DelegateCard announces status icons and shows a dispatched row for an idle agent", async () => {
  const done = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, "a1", "subagent.complete",
    { subagent_id: "s", goal: "Dig", delegation_id: "d1", status: "completed", summary: "ok" }, Date.now());
  await render(<DelegateCard state={done} agentId="a1" delegationId="d1" agentLive={false} />);
  expect(screen.getByLabelText("Completed")).toBeTruthy();
  expect(screen.getByText("Dig")).toBeTruthy();
  await render(<DelegateCard state={EMPTY_ROOM_SUBAGENTS} agentId="a1" delegationId="d2" agentLive={false} />);
  expect(screen.getByText("Background subagents dispatched")).toBeTruthy();
});

it("keeps the sheet open on a child that finished, with its final status and last transcript", async () => {
  const one = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, "a1", "subagent.tool",
    { subagent_id: "sa-0", goal: "Track 0", tool_name: "web_search", tool_preview: "q" }, Date.now());
  const client = { subagentTail: jest.fn().mockResolvedValue({ subagentId: "sa-0", available: true, text: "line 2", truncated: false }) } as never;
  const props = { roomId: "r1", agentName: () => "Forge", client, onSteer: jest.fn(), onStop: jest.fn(), onVisible: jest.fn() };
  await render(<RoomSubagentStrip state={one} {...props} />);
  await fireEvent.press(screen.getByLabelText(/Track 0/));
  await waitFor(() => expect(screen.getByText(/line 2/)).toBeTruthy());
  const done = applySubagentEvent(one, "a1", "subagent.complete", { subagent_id: "sa-0", goal: "Track 0", status: "completed" }, Date.now());
  await screen.rerender(<RoomSubagentStrip state={done} {...props} />);
  expect(screen.getByText("Finished")).toBeTruthy();
  expect(screen.getByText(/line 2/)).toBeTruthy();
  await fireEvent.press(screen.getByLabelText("Stop"));
  expect(props.onStop).not.toHaveBeenCalled();
});
