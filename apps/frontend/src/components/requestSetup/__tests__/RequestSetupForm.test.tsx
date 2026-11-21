import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/analytics", () => ({
  refreshAttribution: vi.fn(),
  attributionProperties: () => ({ first_touch_utm_source: "google", first_touch_landing_path: "/" }),
  client: { get_distinct_id: () => "anon-1" },
}));

import { RequestSetupForm } from "../RequestSetupForm";

function fill() {
  fireEvent.change(screen.getByLabelText("Your name"), { target: { value: "Rosa Diaz" } });
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "rosa@example.test" } });
  fireEvent.change(screen.getByLabelText("Mobile number"), { target: { value: "555 0100" } });
  fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "Example Salon" } });
  fireEvent.click(screen.getByRole("button", { name: "Send to a founder" }));
}

describe("RequestSetupForm", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ id: "r1" }) });
    window.history.pushState({}, "", "?source=home");
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  it("posts a payload the setup-request schema accepts, with ?source= and first-touch attribution", async () => {
    render(<RequestSetupForm />);
    fill();
    await screen.findByText("Thanks, Rosa.");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/setup-requests$/);
    const body = JSON.parse(init.body);
    expect(body).toMatchObject({
      name: "Rosa Diaz",
      phone: "555 0100",
      company: "Example Salon",
      agents: ["other"],
      source_param: "home",
      anonymous_id: "anon-1",
      attribution: { first_touch_utm_source: "google", first_touch_landing_path: "/" },
    });
    expect(screen.getByRole("link", { name: "Set up your assistant" })).toHaveAttribute("href", "/sign-up");
  });

  it("says a founder will get in touch", async () => {
    render(<RequestSetupForm />);
    fill();
    expect(await screen.findByText("Thanks, Rosa.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("A founder reads every request");
  });

  it("says so when the request fails", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 500 });
    render(<RequestSetupForm />);
    fill();
    expect(await screen.findByRole("alert")).toHaveTextContent("did not go through");
  });

  describe("with the waitlist prop", () => {
    function join() {
      fireEvent.change(screen.getByLabelText("Your name"), { target: { value: "Rosa Diaz" } });
      fireEvent.change(screen.getByLabelText("Email"), { target: { value: "rosa@example.test" } });
      fireEvent.click(screen.getByRole("button", { name: "Join the waitlist" }));
    }

    it("posts the waitlist payload without phone or company", async () => {
      render(<RequestSetupForm waitlist="meetings" />);
      expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
      expect(screen.queryByLabelText("Mobile number")).toBeNull();
      join();
      await screen.findByRole("heading", { level: 2, name: "Thanks, Rosa. You're on the list." });
      const body = JSON.parse(fetchMock.mock.calls[0][1].body);
      expect(body).toEqual({
        name: "Rosa Diaz",
        email: "rosa@example.test",
        waitlist: "meetings",
        agents: ["other"],
        attribution: { first_touch_utm_source: "google", first_touch_landing_path: "/" },
        source_param: "home",
        anonymous_id: "anon-1",
      });
      expect(screen.getByRole("status")).toHaveTextContent("We'll email you when Lightbulb Meetings is ready for you.");
    });

    it("a 422 asks to check the email and shows no thanks", async () => {
      fetchMock.mockResolvedValue({ ok: false, status: 422 });
      render(<RequestSetupForm waitlist="meetings" />);
      join();
      expect(await screen.findByRole("alert")).toHaveTextContent("Check your email address and try again.");
      expect(screen.queryByText(/on the list/)).toBeNull();
    });

    it("a double click sends one request", async () => {
      render(<RequestSetupForm waitlist="meetings" />);
      join();
      expect(screen.getByRole("button", { name: "Sending…" })).toBeDisabled();
      fireEvent.click(screen.getByRole("button", { name: "Sending…" }));
      await screen.findByRole("status");
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });

    it("the waitlist thanks has no sign-up link", async () => {
      render(<RequestSetupForm waitlist="meetings" />);
      join();
      await screen.findByRole("status");
      expect(screen.getByRole("status")).toHaveTextContent("Lightbulb Meetings is ready for you");
      expect(screen.queryByRole("link")).toBeNull();
    });
  });
});
