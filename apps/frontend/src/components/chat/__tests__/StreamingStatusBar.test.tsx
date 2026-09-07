/**
 * StreamingStatusBar — run telemetry, not a placeholder.
 *
 * The bar used to render ONLY in silent gaps (`!isStreaming && active`), on
 * the reasoning that while tokens are arriving the transcript is its own
 * status. That argument defeats a duplicate status LABEL; it does not defeat a
 * clock. Elapsed time is orthogonal to the transcript — the transcript says
 * WHAT is happening, this says HOW LONG — and hiding it during streaming
 * removed it from exactly the long runs where "has this hung?" gets asked. It
 * also made a post-run recap structurally impossible, because the bar had
 * unmounted by the time the run settled.
 *
 * It still does NOT narrate the current step: ToolActivityTrace does that in
 * place, in plain language, where the work appears.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StreamingStatusBar } from "../StreamingStatusBar";

const base = { active: true, runPhase: null, runStartedAt: null, lastTurnRecap: null };

describe("StreamingStatusBar", () => {
  it("stays visible while tokens stream — it is telemetry, not a placeholder", () => {
    render(<StreamingStatusBar {...base} runStartedAt={Date.now() - 3000} />);
    expect(screen.getByTestId("streaming-status-bar")).toBeInTheDocument();
  });

  it("renders nothing when no run is active and there is no recap", () => {
    const { container } = render(<StreamingStatusBar {...base} active={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when active is omitted (default off)", () => {
    const { container } = render(<StreamingStatusBar />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a phase label in plain language, not the raw enum", () => {
    render(<StreamingStatusBar {...base} runPhase="starting_model" runStartedAt={Date.now()} />);
    expect(screen.getByText(/Starting the model/i)).toBeInTheDocument();
    expect(screen.queryByText("starting_model")).toBeNull();
  });

  it("names a compaction stall rather than leaving it unexplained", () => {
    render(<StreamingStatusBar {...base} runPhase="compacting" runStartedAt={Date.now()} />);
    expect(screen.getByText(/Compacting the conversation/i)).toBeInTheDocument();
  });

  it("falls back to Working… for an unknown phase rather than printing it", () => {
    render(<StreamingStatusBar {...base} runPhase="some_future_phase" runStartedAt={Date.now()} />);
    expect(screen.getByText(/Working/)).toBeInTheDocument();
    expect(screen.queryByText("some_future_phase")).toBeNull();
  });

  it("falls back to Working… for an EMPTY phase, not an empty label", () => {
    // `??` would not catch this — it only triggers on null/undefined, so an
    // empty-string phase rendered a blank strip. The backend drops phase-less
    // events today, but this component must not depend on a guard living in
    // another service.
    render(<StreamingStatusBar {...base} runPhase="" runStartedAt={Date.now()} />);
    expect(screen.getByText(/Working/)).toBeInTheDocument();
  });

  it("falls back to Working… when there is no phase", () => {
    render(<StreamingStatusBar {...base} runStartedAt={Date.now()} />);
    expect(screen.getByText(/Working/)).toBeInTheDocument();
  });

  it("shows elapsed time and does not announce it", () => {
    render(<StreamingStatusBar {...base} runStartedAt={Date.now() - 65_000} />);
    const elapsed = screen.getByTestId("status-elapsed");
    expect(elapsed).toHaveTextContent("1m 5s");
    expect(elapsed).toHaveAttribute("aria-live", "off");
  });

  it("shows no clock when the run start is unknown", () => {
    render(<StreamingStatusBar {...base} runStartedAt={null} />);
    expect(screen.getByTestId("streaming-status-bar")).toBeInTheDocument();
    expect(screen.queryByTestId("status-elapsed")).toBeNull();
  });

  it("shows the recap after the run settles, with no run active", () => {
    render(<StreamingStatusBar {...base} active={false} lastTurnRecap={{ runtimeMs: 252_000 }} />);
    expect(screen.getByTestId("streaming-status-bar")).toBeInTheDocument();
    expect(screen.getByText(/Done in 4m 12s/i)).toBeInTheDocument();
  });

  it("prefers the live run over a stale recap", () => {
    render(
      <StreamingStatusBar {...base} runStartedAt={Date.now()} lastTurnRecap={{ runtimeMs: 1000 }} />,
    );
    expect(screen.queryByText(/Done in/i)).toBeNull();
  });
});
