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

it("refetches after a resources hint and ignores the superseded response", async () => {
  let old!: (s: LocalSession) => void;
  const get = vi.fn<SessionSource["client"]["getLocalSession"]>().mockImplementationOnce(() => new Promise(resolve => { old = resolve; })).mockResolvedValue(session);
  const view = setup(get);
  await act(async () => { view.rerender(<SessionFocus {...view.props} hintRevision={1} />); });
  expect(screen.getByRole("heading", { name: "Claude Recovered session" })).toBeInTheDocument();
  await act(async () => { old({ ...session, title: "Stale response" }); });
  expect(screen.queryByText("Stale response")).not.toBeInTheDocument();
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


it("restarts a fetch canceled by a cold workspace refresh", async () => {
  let old!: (s: LocalSession) => void;
  const get = vi.fn<SessionSource["client"]["getLocalSession"]>().mockImplementationOnce(() => new Promise(resolve => { old = resolve; })).mockResolvedValue(session);
  const view = setup(get);
  view.rerender(<SessionFocus {...view.props} loading />);
  await act(async () => { view.rerender(<SessionFocus {...view.props} />); });
  expect(get).toHaveBeenCalledTimes(2);
  await act(async () => { old({ ...session, title: "Stale response" }); });
  expect(screen.getByRole("heading", { name: "Claude Recovered session" })).toBeInTheDocument();
});
