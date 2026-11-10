import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ organization: { name: "Salon" } }) }));
vi.mock("next/link", () => ({ default: ({ children, href }: { children: React.ReactNode; href: string }) => <a href={href}>{children}</a> }));
const post = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ useApi: () => ({ post, get: vi.fn() }) }));
const cfg = vi.hoisted(() => ({ value: {} as Record<string, unknown>, issue: undefined as string | undefined, save: vi.fn() }));
vi.mock("../data", async (orig) => ({
  ...(await orig<typeof import("../data")>()),
  useAgentConfig: () => ({ data: cfg.value, save: cfg.save, agents: [{ id: "existing-worker", name: "Founders custom name" }], frontAgentId: "existing-worker", frontAgentIssue: cfg.issue }),
  useInviteAddress: () => "agents@salon.thelightbulb.company",
}));

import { DEFAULT_CONFIG } from "../data";
import { AgentPage } from "../AgentPage";

beforeEach(() => { cfg.issue = undefined; vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {}); vi.spyOn(window, "scrollTo").mockImplementation(() => {}); cfg.value = { ...DEFAULT_CONFIG, name: "Nova", number: "+14155550199" }; cfg.save.mockReset().mockResolvedValue(undefined); });

it("greeting style is optional and never blocks save", async () => {
  vi.useFakeTimers();
  render(<AgentPage />);
  const style = screen.getByLabelText(/Greeting style/);
  fireEvent.change(style, { target: { value: "Warm and brief, mention the sale" } });
  expect(screen.getByText(/Some states require telling callers they are talking to an AI or that calls are recorded/)).toBeTruthy();
  expect(document.querySelector("[data-err='true']")).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(700); });
  expect(cfg.save).toHaveBeenCalledWith(expect.objectContaining({ greeting: "Warm and brief, mention the sale" }));
  vi.useRealTimers();
});

it("unpaid Go live keeps the assistant off and offers the existing payment flow", async () => {
  vi.useFakeTimers();
  cfg.value = { ...cfg.value, live: false, number: "" };
  cfg.save.mockRejectedValueOnce({ status: 402, body: { detail: { code: "voice_payment_required" } } }).mockResolvedValue(undefined);
  render(<AgentPage />);
  fireEvent.click(screen.getByRole("button", { name: "Go live" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(700); });
  expect(screen.getByRole("link", { name: "Activate voice plan" })).toHaveAttribute("href", "/voice/settings#voice-plan-heading");
  expect(screen.getByText("Not live yet")).toBeInTheDocument();
  expect(cfg.save).toHaveBeenLastCalledWith(expect.objectContaining({ live: false }));
  expect(screen.getByRole("button", { name: "Go live" })).toBeInTheDocument();
  vi.useRealTimers();
});

it("a hand-off number outside the US and Canada says why it was not saved", async () => {
  vi.useFakeTimers();
  cfg.save.mockRejectedValueOnce({ status: 422, body: { detail: { code: "handoff_number_not_us" } } });
  render(<AgentPage />);
  fireEvent.change(screen.getByPlaceholderText("Your cell"), { target: { value: "+1 876 555 0123" } });
  await act(async () => { await vi.advanceTimersByTimeAsync(700); });
  expect(screen.getByText(/must be a US or Canadian number/)).toBeInTheDocument();
  vi.useRealTimers();
});

it("hides what does nothing yet, and the waitlist toggle (10 Nov demo ruling)", () => {
  render(<AgentPage />);
  expect(screen.queryByLabelText(/Email me the notes/)).toBeNull();
  expect(screen.queryByText("Square")).toBeNull();
  for (const t of [/Text clients the day before/, /Text back missed callers/, /Text the waitlist/, /waitlist/i, /deposits/]) {
    expect(screen.queryByLabelText(t)).toBeNull();
  }
  expect(screen.queryByText("Not available yet")).toBeNull();
  expect(screen.getByRole("link", { name: "Test call" }).getAttribute("href")).toBe("tel:+14155550199");
});

it("names the business typed at setup, not the workspace", () => {
  cfg.value = { ...cfg.value, business: "Corner Barbers" };
  const { unmount } = render(<AgentPage />);
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Nova · Corner Barbers");
  expect(screen.getByText("Knowledge: what Nova knows about Corner Barbers")).toBeInTheDocument();
  unmount();
  cfg.value = { ...cfg.value, business: "" };
  render(<AgentPage />);
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Nova · Salon"); // the workspace, as a fallback
  expect(screen.getAllByText(/\(415\) 555-0199/).length).toBeGreaterThan(0); // the number, formatted
});

it("a stored square or other shows as the assistant's own calendar", () => {
  cfg.value = { ...cfg.value, bookingsGo: "square" };
  render(<AgentPage />);
  expect((screen.getByLabelText(/Bookings go to/) as HTMLSelectElement).value).toBe("own");
  expect(screen.queryByText(/Another booking system/)).toBeNull();
});

it("agent page shows Check forwarding when keep path", () => {
  cfg.value = { ...cfg.value, numberChoice: { path: "keep" }, forwardFrom: "(415) 555-0177" };
  const { unmount } = render(<AgentPage />);
  expect(screen.getByRole("button", { name: "Check forwarding" })).toBeInTheDocument();
  unmount();
  cfg.value = { ...cfg.value, numberChoice: { path: "new" } };
  render(<AgentPage />);
  expect(screen.queryByRole("button", { name: "Check forwarding" })).toBeNull();
});

it("tells people to invite the workspace's own address, the one that receives", () => {
  // 8 Nov prod Meet: the hard-coded agents@thelightbulb.company bounced (that domain's mail goes to Google)
  render(<AgentPage />);
  expect(screen.getByText("agents@salon.thelightbulb.company")).toBeTruthy();
  expect(screen.queryByText("agents@thelightbulb.company")).toBeNull();
});


it("shows no front-door agent picker", () => {
  render(<AgentPage />);
  expect(screen.queryByLabelText("Receptionist")).toBeNull();
  expect(screen.queryByText(/Workspace default/)).toBeNull();
});

it("saves supported voice and Spanish settings without rewriting the saved legacy default on an unrelated edit", async () => {
  vi.useFakeTimers();
  cfg.value = { ...cfg.value, voice: "Warm", frontAgentId: "existing-worker" };
  render(<AgentPage />);
  expect(screen.getByLabelText("Phone voice")).toHaveValue("marin");
  fireEvent.change(screen.getByLabelText(/Greeting style/), { target: { value: "Brief" } });
  await act(async () => { await vi.advanceTimersByTimeAsync(700); });
  expect(cfg.save).toHaveBeenLastCalledWith(expect.objectContaining({ voice: "Warm", frontAgentId: "existing-worker" }));
  fireEvent.change(screen.getByLabelText("Phone voice"), { target: { value: "quartz" } });
  fireEvent.click(screen.getByLabelText("Also speaks Spanish"));
  await act(async () => { await vi.advanceTimersByTimeAsync(700); });
  expect(cfg.save).toHaveBeenLastCalledWith(expect.objectContaining({ voice: "quartz", spanish: true, frontAgentId: "existing-worker" }));
  expect(screen.getByLabelText("Sample language")).toBeInTheDocument();
  vi.useRealTimers();
});

it("who does a service is a choice from the staff list, stored as names", async () => {
  const { WhoDoesIt } = await import("../AgentPage");
  const set = vi.fn();
  const { rerender } = render(<WhoDoesIt staff={[]} value="Ana" set={set} />);
  expect(screen.getByText("Anyone")).toBeInTheDocument(); // no staff list: one at a time, anyone
  const staff = [{ id: "a1", name: "Ana" }, { id: "m1", name: "Mei" }, { id: "j1", name: "Jo" }];
  rerender(<WhoDoesIt staff={staff} value="Ana" set={set} />); // a name typed before ids still ticks its person
  expect((screen.getByLabelText("Ana") as HTMLInputElement).checked).toBe(true);
  fireEvent.click(screen.getByLabelText("Jo"));
  expect(set).toHaveBeenLastCalledWith("a1, j1"); // stored by id, so a rename keeps it
  fireEvent.click(screen.getByLabelText("Ana"));
  expect(set).toHaveBeenLastCalledWith(""); // none ticked is anyone
});

it("the staff list adds and names people", async () => {
  vi.useFakeTimers();
  render(<AgentPage />);
  fireEvent.click(screen.getByRole("button", { name: /Add a person/ }));
  fireEvent.change(screen.getByLabelText("Staff name"), { target: { value: "Ana" } });
  await act(async () => { await vi.advanceTimersByTimeAsync(700); });
  const saved = cfg.save.mock.lastCall![0].staff;
  expect(saved).toHaveLength(1);
  expect(saved[0].name).toBe("Ana");
  expect(saved[0].id).toMatch(/^[0-9a-f]{8}$/);
  vi.useRealTimers();
});

it("a new closure starts on today's date in the business's time zone, not UTC's", async () => {
  const { Closures } = await import("../AgentPage");
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-11T01:00:00Z")); // 5 pm on 10 Nov in Los Angeles, already 11 Nov in UTC
  const set = vi.fn();
  render(<Closures c={{ ...DEFAULT_CONFIG, timezone: "America/Los_Angeles", holidays: [] }} set={set} />);
  fireEvent.click(screen.getByRole("button", { name: /Add a closed date/ }));
  expect(set).toHaveBeenCalledWith({ holidays: [{ from: "2026-11-10", to: "2026-11-10" }] });
  vi.useRealTimers();
});
