import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { Alert } from "react-native";

jest.mock("@clerk/expo", () => ({ useAuth: () => ({ orgId: "org1", userId: "u1" }) }));

import { MemoriesCard, resetMemoryOverlay } from "../MemoriesCard";

const LIST = {
  private: [{ id: "mem-p", text: "I like tea", createdAt: "2026-10-01", confirmed: false }],
  team: [
    { id: "mem-t1", text: "Ship Fridays", createdAt: "2026-10-01", confirmed: true },
    { id: "mem-t2", text: "Use pnpm", createdAt: "2026-10-01", confirmed: false },
  ],
  truncated: false,
};
const get = jest.fn();
const del = jest.fn();
const post = jest.fn();
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const api = { get, del, post } as any;

beforeEach(() => {
  jest.clearAllMocks();
  resetMemoryOverlay();
  get.mockResolvedValue(LIST);
});

it("renders both sections; Confirmed on confirmed team rows only", async () => {
  await render(<MemoriesCard api={api} />);
  expect(await screen.findByText("I like tea")).toBeTruthy();
  expect(screen.getByText("ONLY YOU")).toBeTruthy();
  expect(screen.getByText("TEAM")).toBeTruthy();
  expect(screen.getByText("Ship Fridays")).toBeTruthy();
  expect(screen.getAllByText("Confirmed")).toHaveLength(1);
});

it("offers Share and Confirm on room rows but not on mirrored log entries", async () => {
  const entry = { id: "mem-e1", text: "Deploy Fridays", createdAt: "2026-10-01", confirmed: false, source: "network" };
  const mine = { id: "mem-e2", text: "Prefers email", createdAt: "2026-10-01", confirmed: false, source: "network" };
  get.mockResolvedValue({ ...LIST, private: [mine, LIST.private[0]], team: [entry, { ...LIST.team[1], source: "room" }] });
  await render(<MemoriesCard api={api} />);
  expect(await screen.findByText("Deploy Fridays")).toBeTruthy();
  expect(screen.getByText("Prefers email")).toBeTruthy();
  expect(screen.getAllByText("Confirm")).toHaveLength(1);
  expect(screen.getAllByText("Share with team")).toHaveLength(1);
});

it("shows an unavailable message when the load fails", async () => {
  get.mockRejectedValue(new Error("502"));
  await render(<MemoriesCard api={api} />);
  expect(await screen.findByText("Memory is unavailable right now.")).toBeTruthy();
});

it("delete asks first, then calls del and drops the row", async () => {
  del.mockResolvedValue(undefined);
  const alert = jest.spyOn(Alert, "alert").mockImplementation(() => {});
  await render(<MemoriesCard api={api} />);
  await screen.findByText("I like tea");
  await fireEvent.press(screen.getAllByLabelText("Delete")[0]);
  expect(alert.mock.calls[0][0]).toBe("Delete this memory?");
  const destructive = (alert.mock.calls[0][2] ?? []).find((b) => b.style === "destructive");
  destructive?.onPress?.();
  await waitFor(() => expect(del).toHaveBeenCalledWith("/memory/mem-p"));
  await waitFor(() => expect(screen.queryByText("I like tea")).toBeNull());
});

it("Share with team moves the row to Team confirmed, without refetching", async () => {
  post.mockResolvedValue({ id: "mem-new" });
  await render(<MemoriesCard api={api} />);
  await screen.findByText("I like tea");
  expect(screen.getAllByLabelText("Share with team")).toHaveLength(1);
  await fireEvent.press(screen.getByLabelText("Share with team"));
  await waitFor(() => expect(post).toHaveBeenCalledWith("/memory/mem-p/share", {}));
  await waitFor(() => expect(screen.queryByLabelText("Share with team")).toBeNull());
  expect(screen.getByText("I like tea")).toBeTruthy();
  expect(screen.getAllByText("Confirmed")).toHaveLength(2);
  expect(get).toHaveBeenCalledTimes(1);
});

it("Confirm shows Confirmed and drops the button, without refetching", async () => {
  post.mockResolvedValue({ id: "mem-t2b" });
  await render(<MemoriesCard api={api} />);
  await screen.findByText("Use pnpm");
  expect(screen.getAllByLabelText("Confirm")).toHaveLength(1);
  await fireEvent.press(screen.getByLabelText("Confirm"));
  await waitFor(() => expect(post).toHaveBeenCalledWith("/memory/mem-t2/confirm", {}));
  await waitFor(() => expect(screen.queryByLabelText("Confirm")).toBeNull());
  expect(screen.getAllByText("Confirmed")).toHaveLength(2);
  expect(get).toHaveBeenCalledTimes(1);
});

it("refetches once at HOLD_MS after a share and renders the updated server lists", async () => {
  post.mockResolvedValue({ id: "mem-new" });
  await render(<MemoriesCard api={api} />);
  await screen.findByText("I like tea");
  jest.useFakeTimers(); // after the first load, so only the post-action timer is faked
  try {
    await fireEvent.press(screen.getByLabelText("Share with team"));
    await waitFor(() => expect(post).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByLabelText("Share with team")).toBeNull());
    expect(get).toHaveBeenCalledTimes(1);
    get.mockResolvedValue({
      ...LIST,
      private: [],
      team: [{ id: "mem-new", text: "I like tea", createdAt: "2026-10-01", confirmed: true }, ...LIST.team],
    });
    await jest.advanceTimersByTimeAsync(90_001);
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getAllByText("I like tea")).toHaveLength(1));
    expect(screen.getAllByText("Confirmed")).toHaveLength(2);
  } finally {
    jest.useRealTimers();
  }
});

it("a refetch inside the lag window keeps the shared fact (overlay)", async () => {
  post.mockResolvedValue({ id: "mem-new" });
  const { unmount } = await render(<MemoriesCard api={api} />);
  await screen.findByText("I like tea");
  await fireEvent.press(screen.getByLabelText("Share with team"));
  await waitFor(() => expect(screen.queryByLabelText("Share with team")).toBeNull());
  unmount();
  await render(<MemoriesCard api={api} />); // server still lists the old private row
  await waitFor(() => expect(screen.getAllByText("Confirmed")).toHaveLength(2));
  expect(screen.getAllByText("I like tea")).toHaveLength(1);
});
