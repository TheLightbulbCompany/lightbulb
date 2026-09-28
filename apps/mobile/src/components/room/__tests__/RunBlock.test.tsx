import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { Linking } from "react-native";
import type { RoomRunView } from "@lightbulb/shared";

import { RunBlock, runCaption } from "../RunBlock";

function makeRun(overrides: Partial<RoomRunView> = {}): RoomRunView {
  return {
    roomId: "room-1",
    agentId: "agent-1",
    runId: "run-1",
    threadRootMessageId: null,
    text: "",
    thinking: "",
    state: "running",
    tools: [],
    ...overrides,
  };
}

/** A promise this test can resolve/reject on its own schedule. */
function deferred<T = unknown>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("RunBlock — per-run stop", () => {
  it("gives each of two concurrently running runs its own independently-tappable Stop", async () => {
    const onAbortA = jest.fn(async () => {});
    const onAbortB = jest.fn(async () => {});
    await render(
      <>
        <RunBlock run={makeRun({ runId: "run-a" })} name="Engineering" onAbort={onAbortA} />
        <RunBlock run={makeRun({ runId: "run-b" })} name="Finance" onAbort={onAbortB} />
      </>,
    );

    await fireEvent.press(screen.getByLabelText("Stop Finance"));
    await waitFor(() => expect(onAbortB).toHaveBeenCalledTimes(1));
    expect(onAbortA).not.toHaveBeenCalled();

    await fireEvent.press(screen.getByLabelText("Stop Engineering"));
    await waitFor(() => expect(onAbortA).toHaveBeenCalledTimes(1));
    // Stopping the second run never re-fired the first's already-settled abort.
    expect(onAbortB).toHaveBeenCalledTimes(1);
  });

  it("does not fire a second abort while the first is still in flight", async () => {
    const gate = deferred<void>();
    const onAbort = jest.fn(() => gate.promise);
    await render(<RunBlock run={makeRun()} name="Engineering" onAbort={onAbort} />);

    await fireEvent.press(screen.getByLabelText("Stop Engineering"));
    await fireEvent.press(screen.getByLabelText("Stopping Engineering"));
    await fireEvent.press(screen.getByLabelText("Stopping Engineering"));
    expect(onAbort).toHaveBeenCalledTimes(1);

    gate.resolve();
    // Accepted is not stopped: it reads "Stopping…" until run_ended takes the control away.
    const stopping = await screen.findByLabelText("Stopping Engineering");
    expect(stopping.props.accessibilityState?.disabled).toBe(true);
    expect(screen.getByText("Stopping…")).toBeTruthy();
    await fireEvent.press(stopping);
    expect(onAbort).toHaveBeenCalledTimes(1);
  });

  it("labels the control Stop, not a bare icon", async () => {
    await render(<RunBlock run={makeRun()} name="Engineering" onAbort={jest.fn(async () => {})} />);
    expect(screen.getByText("Stop")).toBeTruthy();
  });

  it("surfaces a failed stop instead of doing nothing, and stays retryable", async () => {
    const onAbort = jest.fn().mockRejectedValueOnce(new Error("network")).mockResolvedValueOnce(undefined);
    await render(<RunBlock run={makeRun()} name="Engineering" onAbort={onAbort} />);

    const stop = () => screen.getByLabelText("Stop Engineering");
    await fireEvent.press(stop());
    expect(await screen.findByText("This run could not be stopped.")).toBeTruthy();

    await fireEvent.press(stop());
    await waitFor(() => expect(onAbort).toHaveBeenCalledTimes(2));
  });

  it("renders no Stop/steer affordance once the run has left the running state", async () => {
    const onAbort = jest.fn(async () => {});
    await render(
      <RunBlock run={makeRun({ state: "completed", text: "done" })} name="Engineering" onAbort={onAbort} />,
    );
    expect(screen.queryByLabelText("Stop Engineering")).toBeNull();
  });
});

describe("RunBlock — per-run steer", () => {
  it("sends a steer to the run whose input it was typed into, not a sibling run", async () => {
    const onSteerA = jest.fn(async () => {});
    const onSteerB = jest.fn(async () => {});
    await render(
      <>
        <RunBlock run={makeRun({ runId: "run-a" })} name="Engineering" onSteer={onSteerA} />
        <RunBlock run={makeRun({ runId: "run-b" })} name="Finance" onSteer={onSteerB} />
      </>,
    );

    await fireEvent.changeText(screen.getByLabelText("Steer Finance"), "focus on invoicing");
    await fireEvent.press(screen.getByLabelText("Send steering message to Finance"));

    await waitFor(() => expect(onSteerB).toHaveBeenCalledWith("focus on invoicing"));
    expect(onSteerA).not.toHaveBeenCalled();
  });

  it("clears the input on a successful steer", async () => {
    const onSteer = jest.fn(async () => {});
    await render(<RunBlock run={makeRun()} name="Engineering" onSteer={onSteer} />);
    const input = screen.getByLabelText("Steer Engineering");
    await fireEvent.changeText(input, "slow down");
    await fireEvent.press(screen.getByLabelText("Send steering message to Engineering"));
    await waitFor(() => expect(onSteer).toHaveBeenCalledWith("slow down"));
    await waitFor(() => expect(input.props.value).toBe(""));
  });

  it("refuses to submit an empty or whitespace-only steer", async () => {
    const onSteer = jest.fn(async () => {});
    await render(<RunBlock run={makeRun()} name="Engineering" onSteer={onSteer} />);
    await fireEvent.changeText(screen.getByLabelText("Steer Engineering"), "   ");
    await fireEvent.press(screen.getByLabelText("Send steering message to Engineering"));
    expect(onSteer).not.toHaveBeenCalled();
  });

  it("says so when a steer fails, and keeps the draft for a retry", async () => {
    const onSteer = jest.fn(async () => {
      throw new Error("offline");
    });
    await render(<RunBlock run={makeRun()} name="Engineering" onSteer={onSteer} />);
    const input = screen.getByLabelText("Steer Engineering");
    await fireEvent.changeText(input, "keep at it");
    await fireEvent.press(screen.getByLabelText("Send steering message to Engineering"));
    expect(await screen.findByText("This message could not be sent to the run.")).toBeTruthy();
    // The failed draft is not thrown away — the reader can just press send again.
    expect(input.props.value).toBe("keep at it");
  });
});

describe("RunBlock — browsing", () => {
  it("says which site a running agent is browsing and opens the web Browser panel", async () => {
    const open = jest.spyOn(Linking, "openURL").mockResolvedValue(true);
    const tools = [
      { id: "t1", name: "browser_navigate", args: { url: "https://www.example.com/a" }, result: null, status: "done" as const },
    ];
    const view = await render(<RunBlock run={makeRun({ tools })} name="Friday" />);
    await fireEvent.press(screen.getByLabelText("Friday is browsing example.com. Open the shared browser on the web"));
    expect(open).toHaveBeenCalledWith(expect.stringMatching(/\/chat\?panel=browser$/));
    await view.rerender(<RunBlock run={makeRun({ tools, state: "completed" })} name="Friday" />);
    expect(screen.queryByText(/is browsing/)).toBeNull();
    open.mockRestore();
  });
});

const runningRun: RoomRunView = {
  roomId: "room-1", agentId: "ceo", runId: "run-1", triggerMessageId: "m1", threadRootMessageId: null,
  afterSeq: 1, text: "", thinking: "", state: "running", tools: [],
};

describe("RunBlock bubbles", () => {
  it("shows a working bubble with the run's caption before any text", async () => {
    await render(<RunBlock run={{ ...runningRun, text: "", tools: [] }} name="CEO" />);
    expect(screen.getByLabelText("CEO: CEO is working")).toBeTruthy();
  });

  it("captions the tool in use, and a browsing run by its site", () => {
    const tool = { id: "t", name: "Bash", args: { command: "ls -la" }, result: null, status: "running" as const };
    expect(runCaption({ ...runningRun, tools: [tool] }, "CEO")).toBe("Running ls -la");
    const nav = { id: "n", name: "browser_navigate", args: { url: "https://www.example.com/x" }, result: null, status: "done" as const };
    expect(runCaption({ ...runningRun, tools: [nav] }, "CEO")).toBe("Browsing example.com");
  });

  it("folds tools into one chip that expands to the pills", async () => {
    const done = { id: "t", name: "Bash", args: {}, result: null, status: "done" as const };
    await render(<RunBlock run={{ ...runningRun, text: "Rolling the fleet", tools: [done, { ...done, id: "u" }] }} name="CEO" />);
    expect(screen.getByText("Rolling the fleet")).toBeTruthy();
    const chip = screen.getByLabelText("Running 2 tools");
    expect(screen.queryByText("Ran ×2")).toBeNull();
    await fireEvent.press(chip);
    // Two identical calls in a row read as one pill with a count.
    expect(screen.getByText("Ran ×2")).toBeTruthy();
  });

  it("says a stopped run stopped", async () => {
    await render(<RunBlock run={{ ...runningRun, state: "aborted", forcedStop: true, text: "" }} name="CEO" />);
    expect(screen.getByText("Stopped — may finish its current step")).toBeTruthy();
  });
});
