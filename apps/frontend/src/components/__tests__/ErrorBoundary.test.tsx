// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const captureException = vi.hoisted(() => vi.fn());
vi.mock("@/lib/analytics", () => ({ captureException }));

import { ErrorBoundary, isChunkLoadError, REPORT_DELAY_MS } from "@/components/ErrorBoundary";

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

it("a second throw shows the screen, with an email link to support, and both are reported", () => {
  function Boom(): never {
    throw new Error("always");
  }
  render(
    <ErrorBoundary>
      <Boom />
    </ErrorBoundary>,
  );
  expect(screen.getByText("Something went wrong")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Email support" })).toHaveAttribute("href", "mailto:support@thelightbulb.company");
  vi.advanceTimersByTime(REPORT_DELAY_MS);
  expect(captureException).toHaveBeenCalledTimes(2);
});

it("recognises chunk failures and nothing else", () => {
  expect(isChunkLoadError(new TypeError("Failed to fetch dynamically imported module: /x.js"))).toBe(true);
  expect(isChunkLoadError(Object.assign(new Error("x"), { name: "ChunkLoadError" }))).toBe(true);
  expect(isChunkLoadError(new Error("boom"))).toBe(false);
});

it("a chunk error reloads the page once a minute and shows no error screen or report", () => {
  const reload = vi.fn();
  vi.stubGlobal("location", { ...window.location, reload, pathname: "/voice" });
  sessionStorage.clear();
  function Stale(): never {
    throw Object.assign(new Error("Loading chunk 123 failed."), { name: "ChunkLoadError" });
  }
  const mount = () => render(<ErrorBoundary><Stale /></ErrorBoundary>).unmount();
  mount();
  expect(reload).toHaveBeenCalledTimes(1);
  mount();
  expect(reload).toHaveBeenCalledTimes(1);
  vi.advanceTimersByTime(REPORT_DELAY_MS);
  expect(captureException).not.toHaveBeenCalled();
  vi.unstubAllGlobals();
});
