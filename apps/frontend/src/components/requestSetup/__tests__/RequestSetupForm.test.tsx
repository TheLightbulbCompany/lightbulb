import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("source=home"),
}));
vi.mock("@/lib/analytics", () => ({
  refreshAttribution: vi.fn(),
  attributionProperties: () => ({ first_touch_utm_source: "google", first_touch_landing_path: "/" }),
  client: { get_distinct_id: () => "anon-1" },
}));

import { RequestSetupForm } from "../RequestSetupForm";

function fill(teamSize: string) {
  fireEvent.change(screen.getByLabelText("Your name"), { target: { value: "Rosa Diaz" } });
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "rosa@example.test" } });
  fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "Example Salon" } });
  fireEvent.click(screen.getByLabelText(teamSize));
  fireEvent.change(screen.getByLabelText("Where do your bookings live today?"), { target: { value: "Square" } });
  fireEvent.change(screen.getByLabelText("What happens to your calls today?"), {
    target: { value: "I miss calls with clients in the chair" },
  });
  fireEvent.change(screen.getByLabelText("Your website (optional)"), { target: { value: "examplesalon.test" } });
  fireEvent.change(screen.getByLabelText("How did you first hear about us?"), { target: { value: "search" } });
  fireEvent.click(screen.getByRole("button", { name: "Send to a founder" }));
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

  it("posts a payload the setup-request schema accepts, with ?source= and first-touch attribution", async () => {
    render(<RequestSetupForm />);
    fill("3–5");
    await screen.findByText("Thanks, Rosa.");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/setup-requests$/);
    const body = JSON.parse(init.body);
    expect(body).toMatchObject({
      name: "Rosa Diaz",
      company: "Example Salon",
      team_size: "3-5",
      agents: ["other"],
      agents_other: "Bookings: Square",
      first_handoff: "I miss calls with clients in the chair",
      thread_link: "examplesalon.test",
      source: "search",
      source_param: "home",
      anonymous_id: "anon-1",
      attribution: { first_touch_utm_source: "google", first_touch_landing_path: "/" },
    });
    expect(screen.getByRole("link", { name: "Set up your assistant" })).toHaveAttribute("href", "/sign-up");
  });

  it("thanks a one-person shop the same way", async () => {
    render(<RequestSetupForm />);
    fill("Just me");
    expect(await screen.findByText("Thanks, Rosa.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("A founder reads every request");
  });

  it("says so when the request fails", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 500 });
    render(<RequestSetupForm />);
    fill("6–15");
    expect(await screen.findByRole("alert")).toHaveTextContent("did not go through");
  });
});
