import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { LogRow, type LogEntry } from "@/components/admin/LogRow";

const baseEntry: LogEntry = {
  timestamp: "2026-04-18T12:00:00Z",
  level: "ERROR",
  message: "Container failed to provision: ECS task could not start",
  correlation_id: "corr-abc-123",
  raw_json: {
    request_id: "req-123",
    user_id: "user_test_123",
    error: "ResourceInitializationError",
  },
};

describe("LogRow", () => {
  it("renders collapsed by default with timestamp + level + message", () => {
    render(<LogRow entry={baseEntry} />);

    // Admin timestamps render relatively (#1140); >7d old falls back to the
    // date, and the absolute ISO stays on the <time> element's attributes.
    const when = screen.getByText("2026-04-18");
    expect(when.tagName).toBe("TIME");
    expect(when.getAttribute("dateTime")).toBe(new Date(baseEntry.timestamp).toISOString());
    expect(screen.getByText("ERROR")).toBeInTheDocument();
    expect(screen.getByText(baseEntry.message)).toBeInTheDocument();
    // Collapsed: raw json and correlation id are not visible.
    expect(screen.queryByText(/correlation_id:/)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/ResourceInitializationError/),
    ).not.toBeInTheDocument();
  });

  it("expands to show full raw_json on click", async () => {
    const user = userEvent.setup();
    render(<LogRow entry={baseEntry} />);

    await user.click(
      screen.getByRole("button", { name: /Expand log entry/i }),
    );

    expect(screen.getByText(/correlation_id:/)).toBeInTheDocument();
    expect(
      screen.getByText(/ResourceInitializationError/),
    ).toBeInTheDocument();
  });

  it("displays correlation_id when present", async () => {
    const user = userEvent.setup();
    render(<LogRow entry={baseEntry} />);

    await user.click(
      screen.getByRole("button", { name: /Expand log entry/i }),
    );

    expect(screen.getByText("corr-abc-123")).toBeInTheDocument();
  });

  it("level badge has appropriate color class for ERROR", () => {
    render(<LogRow entry={baseEntry} />);
    const badge = screen.getByText("ERROR");
    expect(badge.className).toMatch(/text-err/);
  });
});
