import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const get = vi.fn();
const put = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, put }) }));

import { DailySummaryCard } from "../DailySummaryCard";

beforeEach(() => {
  get.mockReset();
  put.mockReset();
});

describe("DailySummaryCard", () => {
  it("shows the stored pref and saves a toggle", async () => {
    get.mockResolvedValue({ timezone: "Europe/Paris", dailySummary: true });
    put.mockResolvedValue({ timezone: "Europe/Paris", dailySummary: false });
    render(<DailySummaryCard />);
    const box = (await screen.findByLabelText("Daily summary"));
    expect(box).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText(/Europe\/Paris/)).toBeTruthy();
    fireEvent.click(box);
    await waitFor(() => expect(put).toHaveBeenCalledWith("/users/me/prefs", { dailySummary: false }));
    await waitFor(() => expect(box).toHaveAttribute("aria-checked", "false"));
  });

  it("saves the away-email switch on its own key", async () => {
    get.mockResolvedValue({ timezone: "Europe/Paris", dailySummary: true, awayEmail: true });
    put.mockResolvedValue({ timezone: "Europe/Paris", dailySummary: true, awayEmail: false });
    render(<DailySummaryCard />);
    const box = (await screen.findByLabelText("Email me when I'm away"));
    expect(box).toHaveAttribute("aria-checked", "true");
    fireEvent.click(box);
    await waitFor(() => expect(put).toHaveBeenCalledWith("/users/me/prefs", { awayEmail: false }));
    await waitFor(() => expect(box).toHaveAttribute("aria-checked", "false"));
  });

  it("renders nothing until the prefs load", () => {
    get.mockReturnValue(new Promise(() => {}));
    const { container } = render(<DailySummaryCard />);
    expect(container.innerHTML).toBe("");
  });
});
