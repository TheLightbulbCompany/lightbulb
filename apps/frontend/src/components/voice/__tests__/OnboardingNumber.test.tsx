import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const post = vi.hoisted(() => vi.fn());
const get = vi.hoisted(() => vi.fn());
vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({}) }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ post, get }) }));
vi.mock("@/hooks/useConnections", () => ({ useConnections: () => ({ connections: [], startOAuth: vi.fn() }) }));

import { Onboarding } from "../Onboarding";
import { DEFAULT_CONFIG } from "../data";

const at = (step: number, over = {}) => ({ ...DEFAULT_CONFIG, onboardingStep: step, ...over });
beforeEach(() => { post.mockReset(); get.mockReset(); });

it("step 4 keep path shows verizon missed code with number", () => {
  render(<Onboarding config={at(4, { number: "+14842079294" })} save={vi.fn()} />);
  expect(screen.getByText("*714842079294")).toBeInTheDocument();
  expect(screen.getByText("*73")).toBeInTheDocument();
  expect(screen.getByText(/Live Voicemail/)).toBeInTheDocument();
  expect(screen.getByText(/forwarding charges/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Check forwarding" })).toBeInTheDocument();
});

it("step 4 without a number says it appears at Go live", () => {
  render(<Onboarding config={at(4)} save={vi.fn()} />);
  expect(screen.getByText(/appears at Go live/)).toBeInTheDocument();
});

it("step 4 new path posts area code", async () => {
  post.mockResolvedValue({ numberChoice: { path: "new", areaCode: "415" } });
  render(<Onboarding config={at(4)} save={vi.fn()} />);
  fireEvent.click(screen.getByLabelText(/Get a new number/));
  fireEvent.change(screen.getByLabelText("Area code"), { target: { value: "415" } });
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  await waitFor(() => expect(post).toHaveBeenCalledWith("/voice/number", { path: "new", areaCode: "415" }));
});

it("numberChoice survives the next save", async () => {
  const choice = { path: "keep", areaCode: null, carrier: "verizon", mode: "missed" };
  post.mockResolvedValue({ numberChoice: choice });
  const save = vi.fn().mockResolvedValue(undefined);
  render(<Onboarding config={at(4)} save={save} />);
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ numberChoice: choice, onboardingStep: 5 })));
  fireEvent.click(await screen.findByRole("button", { name: "Go live" }));
  await waitFor(() => expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ numberChoice: choice, live: true })));
});

it("go live 409 shows retry message", async () => {
  const save = vi.fn().mockRejectedValue({ status: 409, body: { detail: { code: "number_unavailable" } } });
  render(<Onboarding config={at(5)} save={save} />);
  fireEvent.click(screen.getByRole("button", { name: "Go live" }));
  expect(await screen.findByText(/try Go live again from the Agent page/)).toBeInTheDocument();
});

it("check forwarding shows tick when ok", async () => {
  vi.useFakeTimers();
  post.mockResolvedValue({ ok: true });
  get.mockResolvedValueOnce({ config: { forwardingOk: false } }).mockResolvedValue({ config: { forwardingOk: true } });
  render(<Onboarding config={at(4, { number: "+14842079294", forwardFrom: "(415) 555-0177" })} save={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Check forwarding" }));
  await vi.advanceTimersByTimeAsync(6000);
  vi.useRealTimers();
  expect(post).toHaveBeenCalledWith("/voice/forward-check", { forwardFrom: "(415) 555-0177" });
  expect(get).toHaveBeenCalledTimes(2);
  expect(await screen.findByText(/Forwarding works/)).toBeInTheDocument();
});

it("check forwarding says when it is not switched on, and when the call never came", async () => {
  post.mockResolvedValue({ ok: false, reason: "outbound_not_configured" });
  render(<Onboarding config={at(4, { number: "+14842079294", forwardFrom: "(415) 555-0177" })} save={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Check forwarding" }));
  expect(await screen.findByText("Forwarding check isn't switched on yet")).toBeInTheDocument();
  vi.useFakeTimers();
  post.mockResolvedValue({ ok: true });
  get.mockResolvedValue({ config: {} });
  fireEvent.click(screen.getByRole("button", { name: "Check forwarding" }));
  await vi.advanceTimersByTimeAsync(41_000);
  vi.useRealTimers();
  expect(get).toHaveBeenCalledTimes(13);
  expect(await screen.findByText(/didn't see the call arrive/)).toBeInTheDocument();
});
