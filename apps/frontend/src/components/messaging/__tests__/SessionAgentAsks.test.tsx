// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@lightbulb/shared";
import { afterEach, expect, it, vi } from "vitest";
import { SessionAgentAsks } from "../SessionAgentAsks";

const api = vi.hoisted(() => ({ put: vi.fn(async () => ({})) }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));

const ask = { requestId: "r1", sessionId: "s", kind: "send" as const, text: "ship the fix", agentName: "Friday", state: "awaiting_approval" as const };
const make = (decide = vi.fn(async () => ({ ...ask, state: "pending" as const }))) => ({
  client: { listAgentAsks: vi.fn(async () => [ask]), decideAgentAsk: decide },
  decide,
});
afterEach(() => { cleanup(); api.put.mockClear(); });

it("shows who wants to message which session, the text, and three buttons", async () => {
  render(<SessionAgentAsks client={make().client} sessionId="s" title="Fix login" />);
  expect(await screen.findByText("Friday wants to message Fix login")).toBeInTheDocument();
  expect(screen.getByText("ship the fix")).toBeInTheDocument();
  for (const name of ["Send", "Decline", "Always allow"]) expect(screen.getByRole("button", { name })).toBeInTheDocument();
});

it("Send and Decline answer the ask and clear it", async () => {
  const { client, decide } = make();
  render(<SessionAgentAsks client={client} sessionId="s" title="Fix login" />);
  await userEvent.click(await screen.findByRole("button", { name: "Decline" }));
  expect(decide).toHaveBeenCalledWith("s", "r1", "decline");
  await waitFor(() => expect(screen.queryByText("ship the fix")).toBeNull());
  expect(api.put).not.toHaveBeenCalled();
});

it("Always allow sends this one and turns on the account switch", async () => {
  const { client, decide } = make();
  render(<SessionAgentAsks client={client} sessionId="s" title="Fix login" />);
  await userEvent.click(await screen.findByRole("button", { name: "Always allow" }));
  expect(decide).toHaveBeenCalledWith("s", "r1", "send");
  expect(api.put).toHaveBeenCalledWith("/users/me/prefs", { agentSendWithoutAsking: true });
});

it("Always allow sets the pref first: a failing pref write sends nothing", async () => {
  const { client, decide } = make();
  api.put.mockRejectedValueOnce(new Error("down"));
  render(<SessionAgentAsks client={client} sessionId="s" title="Fix login" />);
  await userEvent.click(await screen.findByRole("button", { name: "Always allow" }));
  expect(await screen.findByText("Could not save the setting. Nothing was sent.")).toBeInTheDocument();
  expect(decide).not.toHaveBeenCalled();
});

it("a failing send after the pref saved says so", async () => {
  const { client } = make(vi.fn(async () => { throw new Error("down"); }));
  render(<SessionAgentAsks client={client} sessionId="s" title="Fix login" />);
  await userEvent.click(await screen.findByRole("button", { name: "Always allow" }));
  expect(await screen.findByText("Setting saved, but this request was not sent. Try again.")).toBeInTheDocument();
  expect(api.put).toHaveBeenCalled();
});

it("Always allow on a 409 still keeps the pref", async () => {
  const { client } = make(vi.fn(async () => { throw new ApiError(409, {}); }));
  render(<SessionAgentAsks client={client} sessionId="s" title="Fix login" />);
  await userEvent.click(await screen.findByRole("button", { name: "Always allow" }));
  expect(await screen.findByText("Already answered")).toBeInTheDocument();
  expect(api.put).toHaveBeenCalledWith("/users/me/prefs", { agentSendWithoutAsking: true });
});

it("refetches on a live event for its session, arms no timer, and ignores other sessions", async () => {
  const timer = vi.spyOn(globalThis, "setInterval");
  let emit: (e: unknown) => void = () => {};
  const onEvent = (h: (e: never) => void) => { emit = h as never; return () => {}; };
  const { client } = make();
  render(<SessionAgentAsks client={client} sessionId="s" title="Fix login" onEvent={onEvent as never} />);
  await screen.findByText("ship the fix");
  expect(client.listAgentAsks).toHaveBeenCalledTimes(1);
  emit({ type: "workspace.session.attention", payload: { sessionId: "other" } });
  expect(client.listAgentAsks).toHaveBeenCalledTimes(1);
  emit({ type: "workspace.session.attention", payload: { sessionId: "s" } });
  await waitFor(() => expect(client.listAgentAsks).toHaveBeenCalledTimes(2));
  emit({ type: "workspace.sessions.changed", payload: {} });
  await waitFor(() => expect(client.listAgentAsks).toHaveBeenCalledTimes(3));
  expect(timer.mock.calls.filter(([, ms]) => (ms ?? 0) >= 1000)).toEqual([]); // waitFor polls at 50 ms; the strip armed none
  timer.mockRestore();
});

it("a 409 says Already answered and removes the buttons", async () => {
  const { client } = make(vi.fn(async () => { throw new ApiError(409, {}); }));
  render(<SessionAgentAsks client={client} sessionId="s" title="Fix login" />);
  await userEvent.click(await screen.findByRole("button", { name: "Send" }));
  expect(await screen.findByText("Already answered")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
});

it("renders nothing when no ask is waiting", async () => {
  const client = { listAgentAsks: vi.fn(async () => []), decideAgentAsk: vi.fn() };
  const { container } = render(<SessionAgentAsks client={client} sessionId="s" title="Fix login" />);
  await waitFor(() => expect(client.listAgentAsks).toHaveBeenCalled());
  expect(container).toBeEmptyDOMElement();
});
