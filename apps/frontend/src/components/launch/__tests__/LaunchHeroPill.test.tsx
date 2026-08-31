import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LaunchHeroPill } from "../LaunchHeroPill";

const info = (over: Record<string, unknown> = {}) => ({
  phase: "open",
  sheet_closes_at: "2026-09-30T16:00:00+00:00",
  competition_start_at: "2026-10-01T16:00:00+00:00",
  competition_end_at: "2026-12-01T16:00:00+00:00",
  signups: 1234,
  comp_slots: 200,
  referral_jump: 10,
  ...over,
});

describe("LaunchHeroPill", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders nothing before the fetch resolves, and nothing on a non-200", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 404 });
    vi.stubGlobal("fetch", fetchMock);
    const { container } = render(<LaunchHeroPill />);
    expect(container).toBeEmptyDOMElement();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when the fetch throws", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("network blip"));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = render(<LaunchHeroPill />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("open: month-free copy with comp_slots, linking /launch — and never a pot", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => info() }),
    );
    render(<LaunchHeroPill />);
    const link = await screen.findByRole("link", { name: /launch competition/i });
    expect(link).toHaveAttribute("href", "/launch");
    expect(link).toHaveTextContent("first 200 get a month free");
    expect(link).toHaveTextContent("see the rules");
    expect(link.textContent).not.toMatch(/pot|\$/);
  });

  it("competition: live-now copy pointing at the leaderboard", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => info({ phase: "competition" }) }),
    );
    render(<LaunchHeroPill />);
    const link = await screen.findByRole("link", { name: /launch competition/i });
    expect(link).toHaveTextContent("live now");
    expect(link).toHaveTextContent("leaderboard");
  });

  it.each([["closed"], ["ended"]])("renders nothing for the %s phase", async (phase) => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => info({ phase }) });
    vi.stubGlobal("fetch", fetchMock);
    const { container } = render(<LaunchHeroPill />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
