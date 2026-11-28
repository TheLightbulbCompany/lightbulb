import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getSpark = vi.fn();
vi.mock("@clerk/nextjs/server", () => ({ auth: async () => ({ getToken: async () => "t" }) }));
vi.mock("../../_lib/api", () => ({ getSpark: (...a: unknown[]) => getSpark(...a) }));
vi.mock("../charts", () => ({ DoersChart: () => null }));
vi.mock("../sections", () => {
  const stub = (name: string) => {
    const C = () => <div>{name} ok</div>;
    C.displayName = name;
    return C;
  };
  return Object.fromEntries(
    ["Channels", "Cohorts", "Funnel", "Headline", "Invites", "Money", "People", "Reliability", "Setup", "Signals", "Sources", "Trials"].map((n) => [n, stub(n)]),
  );
});

import AdminSparkPage from "../page";

describe("spark page", () => {
  beforeEach(() => getSpark.mockReset());

  it("shows the error banner and no figures when queries failed", async () => {
    getSpark.mockResolvedValue({
      data: null,
      source_state: "partial",
      errors: [{ domain: "posthog:installs", reason: "timeout" }],
    });
    render(await AdminSparkPage());
    expect(screen.getByText(/posthog:installs: timeout/)).toBeTruthy();
    expect(screen.queryByText("Headline")).toBeNull();
  });

  it("shows the banner when the read itself failed", async () => {
    getSpark.mockResolvedValue(null);
    render(await AdminSparkPage());
    expect(screen.getByText(/nothing below is trustworthy/)).toBeTruthy();
    expect(screen.queryByText("Headline")).toBeNull();
  });

  it("replaces only the failed section with a placeholder", async () => {
    getSpark.mockResolvedValue({
      data: { generated_at: "now", truncated: false, unavailable: ["money"] },
      source_state: "partial",
      errors: [{ domain: "posthog:subs", reason: "timeout" }],
    });
    render(await AdminSparkPage());
    expect(screen.getByText("couldn't load")).toBeTruthy();
    expect(screen.queryByText("Money ok")).toBeNull();
    expect(screen.getByText("Headline ok")).toBeTruthy();
  });
});
