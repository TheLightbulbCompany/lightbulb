// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const captureException = vi.hoisted(() => vi.fn());
vi.mock("@/lib/analytics", () => ({ captureException }));
vi.mock("@/components/support/SupportTicketDialog", () => ({ SupportTicketDialog: () => null }));

import { ErrorBoundary, REPORT_DELAY_MS } from "@/components/ErrorBoundary";

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(console, "error").mockImplementation(() => {});
  captureException.mockClear();
  window.history.replaceState(null, "", "/voice");
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it("a first render that throws once is retried silently, and still reported with its route", () => {
  // Throws until the boundary has caught it once (React may replay a throwing render itself first).
  const caught = () =>
    vi.mocked(console.error).mock.calls.some(([m]) => m === "Error caught by boundary:");
  function Flaky() {
    if (!caught()) throw new Error("first render after OAuth");
    return <p>loaded</p>;
  }
  render(
    <ErrorBoundary>
      <Flaky />
    </ErrorBoundary>,
  );
  expect(screen.getByText("loaded")).toBeInTheDocument();
  expect(screen.queryByText("Something went wrong")).toBeNull();
  expect(captureException).not.toHaveBeenCalled();
  vi.advanceTimersByTime(REPORT_DELAY_MS);
  expect(captureException).toHaveBeenCalledTimes(1);
  expect(captureException).toHaveBeenCalledWith(
    expect.objectContaining({ message: "first render after OAuth" }),
    { route: "/voice", source: "error_boundary" },
  );
});

it("a second throw shows the screen, with Report this error, and both are reported", () => {
  function Boom(): never {
    throw new Error("always");
  }
  render(
    <ErrorBoundary>
      <Boom />
    </ErrorBoundary>,
  );
  expect(screen.getByText("Something went wrong")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Report this error" })).toBeInTheDocument();
  vi.advanceTimersByTime(REPORT_DELAY_MS);
  expect(captureException).toHaveBeenCalledTimes(2);
});
