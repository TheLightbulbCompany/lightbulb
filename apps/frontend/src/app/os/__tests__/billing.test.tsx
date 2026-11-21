// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import OsBillingPage from "../billing/page";

const s = vi.hoisted(() => ({ query: "" }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(s.query) }));

function visit(query: string, hostname: string) {
  s.query = query;
  const assign = vi.fn();
  vi.stubGlobal("location", { hostname, assign });
  render(<OsBillingPage />);
  return assign;
}

afterEach(() => vi.unstubAllGlobals());

describe("/os/billing", () => {
  it.each([
    ["state=success", "You're subscribed. You can go back to Spark."],
    ["state=cancelled", "No changes made. You can go back to Spark."],
    ["state=already", "You're already subscribed. You can go back to Spark."],
    ["state=portal", "You can go back to Spark."],
    ["state=expired", "This link has expired. Open Spark and try again."],
    ["state=toString", "This link has expired. Open Spark and try again."],
    ["", "This link has expired. Open Spark and try again."],
  ])("%s", (query, line) => {
    visit(query, "thelightbulb.company");
    expect(screen.getByText(line)).toBeInTheDocument();
  });

  it.each([
    ["thelightbulb.company", "spark://account"],
    ["dev.thelightbulb.company", "spark-dev://account"],
    ["localhost", "spark-dev://account"],
  ])("on %s it links to and opens %s once", (hostname, href) => {
    const assign = visit("state=success", hostname);
    expect(screen.getByRole("link", { name: "Open Spark" })).toHaveAttribute("href", href);
    expect(assign).toHaveBeenCalledTimes(1);
    expect(assign).toHaveBeenCalledWith(href);
  });
});
