// @vitest-environment jsdom
import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { trackTeamRunActivity, useActiveTeamRunCount } from "../teamRunActivity";

function Count() {
  return <output aria-label="Active team runs">{useActiveTeamRunCount()}</output>;
}

describe("teamRunActivity", () => {
  it("tracks concurrent runs through terminal status events", () => {
    render(<Count />);
    act(() => {
      trackTeamRunActivity("teams.heartbeat.run.queued", { runId: "run-1" });
      trackTeamRunActivity("teams.heartbeat.run.status", { runId: "run-2", status: "running" });
    });
    expect(screen.getByLabelText("Active team runs")).toHaveTextContent("2");

    act(() => trackTeamRunActivity("teams.heartbeat.run.status", { runId: "run-1", status: "succeeded" }));
    expect(screen.getByLabelText("Active team runs")).toHaveTextContent("1");

    act(() => trackTeamRunActivity("teams.heartbeat.run.status", { runId: "run-2", status: "scheduled_retry" }));
    expect(screen.getByLabelText("Active team runs")).toHaveTextContent("1");

    act(() => trackTeamRunActivity("teams.heartbeat.run.status", { runId: "run-2", status: "interrupted" }));
    expect(screen.getByLabelText("Active team runs")).toHaveTextContent("0");
  });

  it("ignores duplicate, malformed, and unrelated events", () => {
    render(<Count />);
    act(() => {
      trackTeamRunActivity("teams.heartbeat.run.queued", { runId: "run-1" });
      trackTeamRunActivity("teams.heartbeat.run.queued", { runId: "run-1" });
      trackTeamRunActivity("teams.activity.logged", { runId: "run-2" });
      trackTeamRunActivity("teams.heartbeat.run.status", {});
    });
    expect(screen.getByLabelText("Active team runs")).toHaveTextContent("1");
  });
});
