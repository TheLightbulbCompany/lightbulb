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
    const box = (await screen.findByLabelText("Daily summary")) as HTMLInputElement;
    expect(box.checked).toBe(true);
    expect(screen.getByText(/Europe\/Paris/)).toBeTruthy();
    fireEvent.click(box);
    await waitFor(() => expect(put).toHaveBeenCalledWith("/users/me/prefs", { dailySummary: false }));
    await waitFor(() => expect(box.checked).toBe(false));
  });

  it("renders nothing until the prefs load", () => {
    get.mockReturnValue(new Promise(() => {}));
    const { container } = render(<DailySummaryCard />);
    expect(container.innerHTML).toBe("");
  });
});
