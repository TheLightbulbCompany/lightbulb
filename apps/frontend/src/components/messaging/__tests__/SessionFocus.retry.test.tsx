import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ApiError, type LocalSession, type SessionSource } from "@lightbulb/shared";
import { SessionFocus } from "../SessionFocus";

vi.mock("../LocalSessionPanel", () => ({ LocalSessionPanel: ({ session }: { session: LocalSession }) => <p>{session.title}</p> }));
const session = { sessionId: "s1", humanUserId: "alice", provider: "claude", status: "ended", title: "Recovered session" } as LocalSession;
function setup(getLocalSession = vi.fn<SessionSource["client"]["getLocalSession"]>()) {
  const props = { focus: { sessionId: "s1" }, entries: [], loading: false, hintRevision: 0,
    client: { getLocalSession } as unknown as SessionSource["client"],
    resolveSessionOwner: () => ({ ownerName: "Alice", own: false }), onAccessLost: vi.fn(), onClose: vi.fn() };
  return { ...render(<SessionFocus {...props} />), props, getLocalSession };
}
afterEach(() => vi.useRealTimers());

it("recovers an ended session after a transient direct-fetch failure", async () => {
  vi.useFakeTimers();
  const get = vi.fn<SessionSource["client"]["getLocalSession"]>().mockRejectedValueOnce(new TypeError("network")).mockResolvedValue(session);
  setup(get);
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(screen.getByRole("heading", { name: "Claude Recovered session" })).toBeInTheDocument();
  expect(get).toHaveBeenCalledTimes(2);
});

it("stops after three failures and offers an explicit retry", async () => {
  vi.useFakeTimers();
  const get = vi.fn<SessionSource["client"]["getLocalSession"]>().mockRejectedValue(new TypeError("network"));
  setup(get);
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(get).toHaveBeenCalledTimes(3);
  expect(screen.getByText("Could not load this session.")).toBeInTheDocument();
  get.mockResolvedValue(session);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Try again" })); });
  expect(screen.getByRole("heading", { name: "Claude Recovered session" })).toBeInTheDocument();
});

it("does not retry a genuine 404", async () => {
  vi.useFakeTimers();
  const get = vi.fn<SessionSource["client"]["getLocalSession"]>().mockRejectedValue(new ApiError(404, null));
  setup(get);
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(get).toHaveBeenCalledTimes(1);
  expect(screen.getByText("This session is no longer shared.")).toBeInTheDocument();
});

it("keeps a slow fetch alive through several hints and coalesces revalidation", async () => {
  let resolve!: (s: LocalSession) => void;
  const get = vi.fn<SessionSource["client"]["getLocalSession"]>().mockImplementationOnce(() => new Promise(r => { resolve = r; })).mockImplementation(() => new Promise(() => {}));
  const view = setup(get);
  for (let hintRevision = 1; hintRevision <= 4; hintRevision += 1) {
    await act(async () => { view.rerender(<SessionFocus {...view.props} hintRevision={hintRevision} loading={hintRevision % 2 === 1} />); });
  }
  expect(get).toHaveBeenCalledTimes(1);
  await act(async () => { resolve(session); });
  expect(screen.getByRole("heading", { name: "Claude Recovered session" })).toBeInTheDocument();
  expect(get).toHaveBeenCalledTimes(2);
});

it("retries a previous 404 on a later hint", async () => {
  const get = vi.fn<SessionSource["client"]["getLocalSession"]>().mockRejectedValueOnce(new ApiError(404, null)).mockResolvedValue(session);
  const view = setup(get);
  await act(async () => {});
  expect(screen.getByText("This session is no longer shared.")).toBeInTheDocument();
  await act(async () => { view.rerender(<SessionFocus {...view.props} hintRevision={1} />); });
  expect(screen.getByRole("heading", { name: "Claude Recovered session" })).toBeInTheDocument();
});

it("does not reset the three-attempt budget when hints arrive during retry backoff", async () => {
  vi.useFakeTimers();
  const get = vi.fn<SessionSource["client"]["getLocalSession"]>().mockRejectedValue(new TypeError("network"));
  const view = setup(get);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  for (let hintRevision = 1; hintRevision <= 4; hintRevision += 1) {
    await act(async () => { view.rerender(<SessionFocus {...view.props} hintRevision={hintRevision} />); });
  }
  expect(get).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(screen.getByText("Could not load this session.")).toBeInTheDocument();
  // Three attempts finish, then the four hints cause only one queued revalidation.
  expect(get).toHaveBeenCalledTimes(4);
});

it("fetches again on A → B → A and ignores old requests", async () => {
  let old!: (s: LocalSession) => void;
  const get = vi.fn<SessionSource["client"]["getLocalSession"]>().mockImplementationOnce(() => new Promise(resolve => { old = resolve; })).mockResolvedValue(session);
  const view = setup(get);
  await act(async () => { view.rerender(<SessionFocus {...view.props} focus={{ sessionId: "s2" }} />); });
  await act(async () => { view.rerender(<SessionFocus {...view.props} />); });
  expect(get.mock.calls.map(([id]) => id)).toEqual(["s1", "s2", "s1"]);
  await act(async () => { old({ ...session, title: "Stale response" }); });
  expect(screen.getByRole("heading", { name: "Claude Recovered session" })).toBeInTheDocument();
});

it("cancels a scheduled retry when unmounted", async () => {
  vi.useFakeTimers();
  const get = vi.fn<SessionSource["client"]["getLocalSession"]>().mockRejectedValue(new TypeError("network"));
  const view = setup(get);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  view.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(get).toHaveBeenCalledTimes(1);
});


it("ignores a slow response after unmount", async () => {
  let resolve!: (s: LocalSession) => void;
  const get = vi.fn<SessionSource["client"]["getLocalSession"]>().mockImplementation(() => new Promise(r => { resolve = r; }));
  const view = setup(get);
  view.unmount();
  await act(async () => { resolve(session); });
  expect(screen.queryByText("Recovered session")).not.toBeInTheDocument();
  expect(get).toHaveBeenCalledTimes(1);
});


it.each(["404", "exhaustion"] as const)("does not refetch a settled %s on ordinary workspace polls", async (failure) => {
  vi.useFakeTimers();
  const get = vi.fn<SessionSource["client"]["getLocalSession"]>().mockRejectedValue(failure === "404" ? new ApiError(404, null) : new TypeError("network"));
  const view = setup(get);
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  const calls = failure === "404" ? 1 : 3;
  expect(get).toHaveBeenCalledTimes(calls);
  for (let i = 0; i < 2; i += 1) {
    await act(async () => { view.rerender(<SessionFocus {...view.props} loading />); });
    await act(async () => { view.rerender(<SessionFocus {...view.props} />); });
  }
  expect(get).toHaveBeenCalledTimes(calls);
  get.mockResolvedValue(session);
  await act(async () => {
    if (failure === "404") view.rerender(<SessionFocus {...view.props} hintRevision={1} />);
    else fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  });
  expect(screen.getByRole("heading", { name: "Claude Recovered session" })).toBeInTheDocument();
  expect(get).toHaveBeenCalledTimes(calls + 1);
});
