import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ organization: { name: "Salon" } }) }));
vi.mock("next/link", () => ({ default: ({ children, href }: { children: React.ReactNode; href: string }) => <a href={href}>{children}</a> }));
const cfg = vi.hoisted(() => ({ value: {} as Record<string, unknown>, save: vi.fn() }));
vi.mock("../data", async (orig) => ({
  ...(await orig<typeof import("../data")>()),
  useAgentConfig: () => ({ data: cfg.value, save: cfg.save }),
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
