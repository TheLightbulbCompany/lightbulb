import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LaunchHeroPill } from "../LaunchHeroPill";

const info = (over: Record<string, unknown> = {}) => ({
  phase: "waitlist",
  drop_at: "2026-09-18T16:00:00+00:00",
  competition_start_at: "2026-09-25T16:00:00+00:00",
  competition_end_at: "2026-10-25T16:00:00+00:00",
  signups: 1234,
  competing: 0,
  on_board: 0,
  pot_cents: 103000,
  comp_slots: 220,
  pot_per_entrant_cents: 1000,
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

  it("renders the waitlist copy with the pot and signups formatted, linking to /launch", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => info() }),
    );
    render(<LaunchHeroPill />);
    const link = await screen.findByRole("link", { name: /launch competition/i });
    expect(link).toHaveAttribute("href", "/launch");
    expect(link).toHaveTextContent("$1,030");
    expect(link).toHaveTextContent("1,234 on the list");
    expect(link).toHaveTextContent("see your spot");
  });

  it("renders the activation copy without a signup count", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => info({ phase: "activation" }) }),
    );
    render(<LaunchHeroPill />);
    const link = await screen.findByRole("link", { name: /launch competition/i });
    expect(link).toHaveTextContent("doors are open");
    expect(link).toHaveTextContent("join");
  });

  it("renders the live copy with the competing count formatted", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => info({ phase: "live", competing: 4200 }) }),
    );
    render(<LaunchHeroPill />);
    const link = await screen.findByRole("link", { name: /launch competition is on/i });
    expect(link).toHaveTextContent("4,200 competing");
    expect(link).toHaveTextContent("the board");
  });

  it("renders nothing once the competition has ended", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => info({ phase: "ended" }) });
    vi.stubGlobal("fetch", fetchMock);
    const { container } = render(<LaunchHeroPill />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
