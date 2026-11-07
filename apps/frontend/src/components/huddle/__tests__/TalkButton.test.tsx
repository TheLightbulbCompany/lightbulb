import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { beforeEach, expect, it, vi } from "vitest";

const h = vi.hoisted(() => ({
  huddle: { roomId: null as string | null, joining: null as string | null, join: vi.fn(), leave: vi.fn() },
  rooms: [] as unknown[],
}));
vi.mock("../HuddleProvider", () => ({ useHuddle: () => h.huddle }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: async () => ({ rooms: h.rooms }) }) }));
import { TalkButton } from "../TalkButton";

const dm = { roomId: "dm1", kind: "dm", members: [{ kind: "human" }, { kind: "agent" }] };
const view = () => render(<SWRConfig value={{ provider: () => new Map() }}><TalkButton /></SWRConfig>);

beforeEach(() => { vi.clearAllMocks(); h.huddle.roomId = null; h.huddle.joining = null; h.rooms = [{ roomId: "c1", kind: "channel" }, dm]; });

it("one press goes Live in the DM with the assistant", async () => {
  view();
  const talk = screen.getByRole("button", { name: "Talk to Lightbulb" });
  await waitFor(() => expect(talk).toBeEnabled());
  fireEvent.click(talk);
  expect(h.huddle.join).toHaveBeenCalledWith("dm1", "Lightbulb");
});

it("while Live in the DM it is pressed and a press ends it", async () => {
  h.huddle.roomId = "dm1";
  view();
  const end = await screen.findByRole("button", { name: "End talk with Lightbulb" });
  expect(end).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(end);
  expect(h.huddle.leave).toHaveBeenCalled();
});

it("is off while the DM is unknown or the join is in flight", async () => {
  h.rooms = [{ roomId: "c1", kind: "channel" }];
  const { unmount } = view();
  expect(screen.getByRole("button", { name: "Talk to Lightbulb" })).toBeDisabled();
  unmount();
  h.rooms = [dm]; h.huddle.joining = "dm1";
  view();
  expect(await screen.findByRole("button", { name: "Connecting to Lightbulb" })).toBeDisabled();
});
