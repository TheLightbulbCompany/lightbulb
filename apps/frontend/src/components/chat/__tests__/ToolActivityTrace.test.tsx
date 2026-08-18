import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ToolActivityTrace, type ToolCall } from "@/components/chat/ToolActivityTrace";

function call(over: Partial<ToolCall> & Pick<ToolCall, "id">): ToolCall {
  return {
    name: "read",
    args: { path: "a.ts" },
    result: "ok",
    status: "done",
    ...over,
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("ToolActivityTrace", () => {
  it("renders one trace for consecutive calls, never the raw tool name", () => {
    render(
      <ToolActivityTrace
        calls={[
          call({ id: "1", name: "read", args: { path: "src/connection_pool.py" } }),
          call({ id: "2", name: "bash", args: { command: "npm test" } }),
        ]}
      />,
    );
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText("src/connection_pool.py")).toBeInTheDocument();
    expect(screen.getByText("npm test")).toBeInTheDocument();
    // The bug this component exists to fix: the internal identifier must never
    // reach the user as a label.
    expect(screen.queryByText("read")).not.toBeInTheDocument();
    expect(screen.queryByText("bash")).not.toBeInTheDocument();
  });

  it("reads the CLI-prefixed spelling the same as the native one", () => {
    render(
      <ToolActivityTrace
        calls={[call({ id: "1", name: "mcp__openclaw__web_search", args: { query: "ecs spot" } })]}
      />,
    );
    expect(screen.getByText(/Searched the web/)).toBeInTheDocument();
    expect(screen.queryByText(/mcp__openclaw__/)).not.toBeInTheDocument();
  });

  it("renders an activity pill's own label verbatim, bypassing the tool-name resolver", () => {
    // `name` here is already the human sentence the backend sent — running
    // it through resolveToolDisplay Title-Cases it and can append an
    // unrelated raw id from CONFIG.fallback.detailKeys (the bug this kind
    // flag exists to prevent).
    render(
      <ToolActivityTrace
        calls={[
          call({
            id: "1",
            kind: "activity",
            name: "applied patch to 3 files",
            args: { itemId: "itm_01H0000000000000000000" },
          }),
        ]}
      />,
    );
    expect(screen.getByText("applied patch to 3 files")).toBeInTheDocument();
    expect(screen.queryByText(/Applied Patch/)).not.toBeInTheDocument();
    expect(screen.queryByText(/itm_01H/)).not.toBeInTheDocument();
  });

  it("uses present tense while running and past tense once done", () => {
    const { rerender } = render(
      <ToolActivityTrace calls={[call({ id: "1", status: "running", result: null })]} />,
    );
    expect(screen.getByText("Reading")).toBeInTheDocument();
    rerender(<ToolActivityTrace calls={[call({ id: "1", status: "done" })]} />);
    expect(screen.getByText("Read")).toBeInTheDocument();
  });

  it("folds past three calls when settled, stays open at three or fewer", () => {
    // `call()` defaults to status "done" — settled is derived from that, so
    // a settled trace needs no explicit prop.
    const three = [1, 2, 3].map((i) => call({ id: `${i}`, args: { path: `f${i}.ts` } }));
    const { rerender } = render(<ToolActivityTrace calls={three} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(3);

    const four = [1, 2, 3, 4].map((i) => call({ id: `${i}`, args: { path: `f${i}.ts` } }));
    rerender(<ToolActivityTrace calls={four} />);
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
    expect(screen.getByText(/Read 4 files/)).toBeInTheDocument();
  });

  it("does not fold while the turn is still running", () => {
    // Settled is derived from call status — mark the last call still
    // running so `calls.every(status !== "running")` reads false.
    const calls = [1, 2, 3, 4, 5].map((i) =>
      call({ id: `${i}`, args: { path: `f${i}.ts` }, ...(i === 5 ? { status: "running" as const } : {}) }),
    );
    render(<ToolActivityTrace calls={calls} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
  });

  it("folding is a default, not an override — expanding survives, and re-collapses on request", async () => {
    const calls = [1, 2, 3, 4].map((i) => call({ id: `${i}`, args: { path: `f${i}.ts` } }));
    render(<ToolActivityTrace calls={calls} />);
    await userEvent.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getAllByRole("listitem")).toHaveLength(4);

    // The fold is a two-way affordance, not a one-shot reveal: a still-eligible
    // (settled, over-threshold) trace the user opened can be sent back.
    await userEvent.click(screen.getByRole("button", { name: /collapse/i }));
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
    expect(screen.getByText(/Read 4 files/)).toBeInTheDocument();
  });

  it("keeps a failure visible, in place, with its error inline", () => {
    render(
      <ToolActivityTrace
        calls={[
          call({ id: "1" }),
          call({
            id: "2",
            name: "bash",
            args: { command: "aws ecs describe-capacity-providers" },
            status: "error",
            result: "AccessDeniedException: not authorized",
          }),
          call({ id: "3" }),
        ]}
      />,
    );
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    // Position preserved — a failure does not get hoisted or dropped.
    expect(rows[1]).toHaveTextContent("aws ecs describe-capacity-providers");
    // Error text is inline and not behind a click.
    const errorOutput = screen.getByText(/AccessDeniedException/);
    expect(errorOutput).toBeInTheDocument();
    // ...but height-capped and scrollable, like every other output path in
    // this file — a 300-line stack trace must not flood the transcript.
    expect(errorOutput).toHaveClass("max-h-48", "overflow-auto");
  });

  it("hides sub-2s durations and shows longer ones", () => {
    render(
      <ToolActivityTrace
        calls={[
          call({ id: "fast", startedAt: 1_000, endedAt: 1_040 }),
          call({ id: "slow", startedAt: 1_000, endedAt: 4_100, args: { path: "b.ts" } }),
        ]}
      />,
    );
    expect(screen.queryByText("0.0s")).not.toBeInTheDocument();
    expect(screen.getByText("3.1s")).toBeInTheDocument();
  });

  it("renders without durations when history carries no timing", () => {
    render(<ToolActivityTrace calls={[call({ id: "1" })]} />);
    expect(screen.getByRole("listitem")).toBeInTheDocument();
    expect(screen.queryByText(/\ds$/)).not.toBeInTheDocument();
  });

  it("stops claiming a stalled call is running", () => {
    // Known dropped-RPC failure mode: no tool_end ever arrives. The row must
    // say so rather than leave a gerund spinning forever.
    render(
      <ToolActivityTrace
        calls={[
          call({ id: "1", status: "running", result: null, startedAt: Date.now() - 120_000 }),
        ]}
      />,
    );
    expect(screen.getByText(/outcome unknown/i)).toBeInTheDocument();
  });

  it("expanding a row still reaches the raw args and output", async () => {
    render(
      <ToolActivityTrace
        calls={[call({ id: "1", args: { path: "a.ts", offset: 4 }, result: "line one" })]}
      />,
    );
    await userEvent.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByText(/"offset": 4/)).toBeInTheDocument();
    expect(screen.getByText("line one")).toBeInTheDocument();
  });

  it("renders nothing for an empty turn", () => {
    const { container } = render(<ToolActivityTrace calls={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
