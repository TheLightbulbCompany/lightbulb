import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { applySubagentEvent, EMPTY_ROOM_SUBAGENTS } from "@lightbulb/shared";
import { delegationIdOf } from "@lightbulb/shared";
import { DelegateCard } from "../DelegateCard";

describe("DelegateCard", () => {
  const s = [0, 1].reduce((acc, i) => applySubagentEvent(acc, "a1", i ? "subagent.complete" : "subagent.start",
    { subagent_id: `sa-${i}`, goal: `Track ${i}`, delegation_id: "d-1", model: "gpt-5.6-sol", status: i ? "completed" : undefined, duration_seconds: 12 }, 1000), EMPTY_ROOM_SUBAGENTS);

  it("one row per child of this delegation with status and meta", () => {
    render(<DelegateCard state={s} agentId="a1" delegationId="d-1" agentLive />);
    expect(screen.getByText("Track 0")).toBeInTheDocument();
    expect(screen.getByLabelText("Running")).toBeInTheDocument();
    expect(screen.getByLabelText("Completed")).toBeInTheDocument();
    expect(screen.getAllByText(/gpt-5.6-sol · 12s/)).toHaveLength(2); // both children carry the same model and duration
  });

  it("with no live view, a child that is neither terminal nor live shows the static dispatched dot, never a spinner", () => {
    render(<DelegateCard state={EMPTY_ROOM_SUBAGENTS} agentId="a1" delegationId="d-1" agentLive={false} />);
    expect(screen.getByLabelText("Dispatched")).toBeInTheDocument();
    expect(screen.queryByLabelText("Running")).not.toBeInTheDocument();
  });

  it("delegationIdOf reads a JSON string or an object", () => {
    expect(delegationIdOf('{"status":"dispatched","delegation_id":"d-1"}')).toBe("d-1");
    expect(delegationIdOf({ delegation_id: "d-2" })).toBe("d-2");
    expect(delegationIdOf("oops")).toBeNull();
  });
});
