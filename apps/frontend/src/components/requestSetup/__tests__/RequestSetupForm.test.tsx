import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("source=compare-slack"),
}));
vi.mock("@/lib/analytics", () => ({
  refreshAttribution: vi.fn(),
  attributionProperties: () => ({ first_touch_utm_source: "hn", first_touch_landing_path: "/" }),
  client: { get_distinct_id: () => "anon-1" },
}));

import { RequestSetupForm } from "../RequestSetupForm";

function fill(teamSize: string) {
  fireEvent.change(screen.getByLabelText("Your name"), { target: { value: "Mara Reyes" } });
  fireEvent.change(screen.getByLabelText("Work email"), { target: { value: "mara@acme.test" } });
  fireEvent.change(screen.getByLabelText("Company"), { target: { value: "Acme" } });
  fireEvent.click(screen.getByLabelText(teamSize));
  fireEvent.click(screen.getByLabelText("Codex"));
  fireEvent.change(screen.getByLabelText("What would you hand off first?"), { target: { value: "investor update" } });
  fireEvent.change(screen.getByLabelText("How did you first hear about us?"), { target: { value: "reddit" } });
  fireEvent.click(screen.getByRole("button", { name: "Request my setup" }));
}

describe("RequestSetupForm", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ id: "r1" }) });
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  it("posts the answers with source, ?source= and first-touch attribution", async () => {
    render(<RequestSetupForm />);
    fill("3–5");
    await screen.findByText("Thanks, Mara.");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/setup-requests$/);
    const body = JSON.parse(init.body);
    expect(body).toMatchObject({
      team_size: "3-5",
      agents: ["codex"],
      source: "reddit",
      source_param: "compare-slack",
      anonymous_id: "anon-1",
      attribution: { first_touch_utm_source: "hn", first_touch_landing_path: "/" },
    });
    expect(screen.getByText(/email you within a day/)).toBeInTheDocument();
  });

  it("sends a one- or two-person team to Start free", async () => {
    render(<RequestSetupForm />);
    fill("Just me");
    await waitFor(() => expect(screen.getByRole("link", { name: "Start free" })).toHaveAttribute("href", "/sign-up"));
  });

  it("says so when the request fails", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 500 });
    render(<RequestSetupForm />);
    fill("6–15");
    expect(await screen.findByRole("alert")).toHaveTextContent("did not go through");
  });
});
