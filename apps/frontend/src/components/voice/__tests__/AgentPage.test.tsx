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

it("hides what does nothing yet and shows the texts as off", () => {
  render(<AgentPage />);
  for (const gone of [/Email me the notes/, /Add callers to the waitlist/]) expect(screen.queryByLabelText(gone)).toBeNull();
  expect(screen.queryByText("Square")).toBeNull();
  for (const t of [/Text back missed callers/, /Text the waitlist/]) {
    const box = screen.getByLabelText(t) as HTMLInputElement;
    expect(box.checked).toBe(false); expect(box.disabled).toBe(true);
  }
  expect(screen.getAllByText("Not available yet")).toHaveLength(3);
  expect((screen.getByLabelText(/Text clients the day before/) as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByLabelText(/Text clients the day before/) as HTMLInputElement).checked).toBe(false);
  expect(screen.getByRole("link", { name: "Test call" }).getAttribute("href")).toBe("tel:+14155550199");
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


it("designates an existing worker without renaming it or changing caller settings", async () => {
  vi.useFakeTimers();
  cfg.value = { ...cfg.value, voice: "Warm", spanish: true };
  render(<AgentPage />);
  fireEvent.change(screen.getByLabelText("Receptionist"), { target: { value: "existing-worker" } });
  await act(async () => { await vi.advanceTimersByTimeAsync(700); });
  expect(cfg.save).toHaveBeenCalledWith(expect.objectContaining({ frontAgentId: "existing-worker", name: "Nova", voice: "Warm", spanish: true }));
  expect(screen.getByText(/existing conversations keep their agent/)).toBeInTheDocument();
  vi.useRealTimers();
});


it("keeps an unavailable saved selection visible and offers an existing replacement", () => {
  cfg.value = { ...cfg.value, frontAgentId: "deleted-worker" };
  cfg.issue = "Choose an active front-door agent. Your saved selection is unavailable.";
  render(<AgentPage />);
  expect(screen.getByRole("alert")).toHaveTextContent("Choose an active front-door agent");
  expect(screen.getByLabelText("Receptionist")).toHaveValue("deleted-worker");
  expect(screen.getByRole("option", { name: "Founders custom name" })).toHaveValue("existing-worker");
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
  rerender(<WhoDoesIt staff={["Ana", "Mei", "Jo"]} value="Ana" set={set} />);
  expect((screen.getByLabelText("Ana") as HTMLInputElement).checked).toBe(true);
  fireEvent.click(screen.getByLabelText("Jo"));
  expect(set).toHaveBeenLastCalledWith("Ana, Jo");
  fireEvent.click(screen.getByLabelText("Ana"));
  expect(set).toHaveBeenLastCalledWith(""); // none ticked is anyone
});

it("the staff list adds and names people", async () => {
  vi.useFakeTimers();
  render(<AgentPage />);
  fireEvent.click(screen.getByRole("button", { name: /Add a person/ }));
  fireEvent.change(screen.getByLabelText("Staff name"), { target: { value: "Ana" } });
  await act(async () => { await vi.advanceTimersByTimeAsync(700); });
  expect(cfg.save).toHaveBeenLastCalledWith(expect.objectContaining({ staff: ["Ana"] }));
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
