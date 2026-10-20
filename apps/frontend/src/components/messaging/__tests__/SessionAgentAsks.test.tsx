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
