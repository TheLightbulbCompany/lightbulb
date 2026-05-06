// Tests for MetricCard: label/value/description rendering, icon rendering,
// link wrapping with `to`, plain rendering without `to`, and onClick fallback.

import { describe, test, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { Activity } from "lucide-react";
import { MetricCard } from "@/components/teams/dashboard/MetricCard";

describe("MetricCard", () => {
  test("renders label and value", () => {
    render(<MetricCard icon={Activity} label="Open issues" value={42} />);
    expect(screen.getByText("Open issues")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
  });

  test("renders optional description when provided", () => {
    render(
      <MetricCard
        icon={Activity}
        label="Runs"
        value={3}
        description="last 14 days"
      />
    );
    expect(screen.getByText("last 14 days")).toBeInTheDocument();
  });

  test("does not render description block when omitted", () => {
    render(<MetricCard icon={Activity} label="Runs" value={3} />);
    expect(screen.queryByText(/last 14 days/i)).not.toBeInTheDocument();
  });

  test("renders the icon (via title-less svg from lucide)", () => {
    const { container } = render(
      <MetricCard icon={Activity} label="Open" value={0} />
    );
    // lucide icons render as svg
    expect(container.querySelector("svg")).not.toBeNull();
  });

  test("wraps the card in a link when `to` is provided", () => {
    render(
      <MetricCard
        icon={Activity}
        label="Issues"
        value={5}
        to="/teams/issues"
      />
    );
    const anchor = screen.getByRole("link");
    expect(anchor).toHaveAttribute("href", "/teams/issues");
    expect(anchor).toHaveTextContent("5");
    expect(anchor).toHaveTextContent("Issues");
  });

  test("does not wrap in a link when `to` is omitted", () => {
    render(<MetricCard icon={Activity} label="Issues" value={5} />);
    expect(screen.queryByRole("link")).toBeNull();
  });

  test("invokes onClick when clicked without `to`", () => {
    const onClick = vi.fn();
    const { container } = render(
      <MetricCard icon={Activity} label="Issues" value={5} onClick={onClick} />
    );
    const wrapper = container.firstChild as HTMLElement;
    wrapper.click();
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
