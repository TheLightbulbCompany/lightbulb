import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ organization: { name: "Salon" } }) }));
vi.mock("next/link", () => ({ default: ({ children, href }: { children: React.ReactNode; href: string }) => <a href={href}>{children}</a> }));
const post = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ useApi: () => ({ post, get: vi.fn() }) }));
const cfg = vi.hoisted(() => ({ value: {} as Record<string, unknown>, save: vi.fn() }));
vi.mock("../data", async (orig) => ({
  ...(await orig<typeof import("../data")>()),
  useAgentConfig: () => ({ data: cfg.value, save: cfg.save }),
  useInviteAddress: () => "agents@salon.thelightbulb.company",
}));

import { DEFAULT_CONFIG } from "../data";
import { AgentPage } from "../AgentPage";

beforeEach(() => { cfg.value = { ...DEFAULT_CONFIG, name: "Nova", number: "+14155550199" }; cfg.save.mockReset().mockResolvedValue(undefined); });

it("greeting style is optional and never blocks save", async () => {
  vi.useFakeTimers();
  render(<AgentPage />);
  const style = screen.getByLabelText(/Greeting style/);
  fireEvent.change(style, { target: { value: "Warm and brief, mention the sale" } });
  expect(screen.getByText(/always says its name, that it.s an AI assistant and that the call is recorded/)).toBeTruthy();
  expect(document.querySelector("[data-err='true']")).toBeNull();
  await vi.advanceTimersByTimeAsync(700);
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
  for (const gone of [/Email me the notes/, /Also speaks Spanish/, /Add callers to the waitlist/, /^Voice$/]) expect(screen.queryByLabelText(gone)).toBeNull();
  expect(screen.queryByText("Square")).toBeNull();
  for (const t of [/Text back missed callers/, /Text the waitlist/]) {
    const box = screen.getByLabelText(t) as HTMLInputElement;
    expect(box.checked).toBe(false); expect(box.disabled).toBe(true);
  }
  expect(screen.getAllByText("Off until we finish a consent review")).toHaveLength(2);
  expect((screen.getByLabelText(/Text clients the day before/) as HTMLInputElement).disabled).toBe(false);
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
