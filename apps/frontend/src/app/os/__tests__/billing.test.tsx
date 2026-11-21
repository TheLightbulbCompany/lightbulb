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
    ["thelightbulb.company", "state=success", "spark://account?state=success"],
    ["dev.thelightbulb.company", "state=success", "spark-dev://account?state=success"],
    ["localhost", "state=portal", "spark-dev://account?state=portal"],
    ["thelightbulb.company", "state=cancelled", "spark://account?state=cancelled"],
    ["thelightbulb.company", "state=already", "spark://account?state=already"],
    ["thelightbulb.company", "state=%22%3E%3Cx", "spark://account?state=expired"],
    ["thelightbulb.company", "", "spark://account?state=expired"],
  ])("on %s with %s it links to and opens %s once", (hostname, query, href) => {
    const assign = visit(query, hostname);
    expect(screen.getByRole("link", { name: "Open Spark" })).toHaveAttribute("href", href);
    expect(assign).toHaveBeenCalledTimes(1);
    expect(assign).toHaveBeenCalledWith(href);
  });
});
