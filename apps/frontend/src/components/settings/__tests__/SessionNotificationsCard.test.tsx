import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";

const get = vi.fn();
const put = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, put }) }));

import { SessionNotificationsCard } from "../SessionNotificationsCard";

const card = () => render(<SWRConfig value={{ provider: () => new Map() }}><SessionNotificationsCard /></SWRConfig>);
const DEFAULTS = { waiting: "banner", finished: "banner", attention: "badge", onlyUnfocused: false, sound: true };

beforeEach(() => {
  get.mockReset();
  put.mockReset();
});

describe("SessionNotificationsCard", () => {
  it("shows today's behaviour when nothing is stored", async () => {
    get.mockResolvedValue({ timezone: "UTC" });
    card();
    // Our select, not the native one (W13-10): the trigger shows the level.
    expect(await screen.findByRole("combobox", { name: "Waiting on you" })).toHaveTextContent("Banner");
    expect(screen.queryByLabelText("Needs attention")).toBeNull(); // hand-off notes are not a setting any more
    expect(screen.getByLabelText("Sound")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByLabelText("Only when Lightbulb isn't focused")).toHaveAttribute("aria-checked", "false");
  });

  it("saves the whole object with the one change", async () => {
    get.mockResolvedValue({ sessionNotifications: DEFAULTS });
    put.mockResolvedValue({ sessionNotifications: { ...DEFAULTS, finished: "off" } });
    card();
    const finished = await screen.findByRole("combobox", { name: "Finished" });
    await userEvent.click(finished);
    await userEvent.click(await screen.findByRole("option", { name: "Off" }));
    await waitFor(() => expect(put).toHaveBeenCalledWith("/users/me/prefs", { sessionNotifications: { ...DEFAULTS, finished: "off" } }));
    await waitFor(() => expect(finished).toHaveTextContent("Off"));

    put.mockResolvedValue({ sessionNotifications: { ...DEFAULTS, finished: "off", onlyUnfocused: true } });
    fireEvent.click(screen.getByLabelText("Only when Lightbulb isn't focused"));
    await waitFor(() => expect(put).toHaveBeenLastCalledWith("/users/me/prefs",
      { sessionNotifications: { ...DEFAULTS, finished: "off", onlyUnfocused: true } }));
  });
});
