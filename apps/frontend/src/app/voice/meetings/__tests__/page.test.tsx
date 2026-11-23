import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const cfg = vi.hoisted(() => ({ value: {} as Record<string, unknown>, invite: undefined as string | undefined, save: vi.fn() }));
vi.mock("@/components/voice/data", async (orig) => ({
  ...(await orig<typeof import("@/components/voice/data")>()),
  useAgentConfig: () => ({ data: cfg.value, save: cfg.save }),
  useInviteAddress: () => cfg.invite,
}));

import { DEFAULT_CONFIG } from "@/components/voice/data";
import Page from "../page";

beforeEach(() => {
  cfg.value = { ...DEFAULT_CONFIG, name: "Nova", meetings: { followUps: true } };
  cfg.invite = "agents@salon.thelightbulb.company";
  cfg.save.mockReset().mockResolvedValue(undefined);
});

it("tells people to invite the workspace's own address, the one that receives", () => {
  // 8 Nov prod Meet: the hard-coded agents@thelightbulb.company bounced (that domain's mail goes to Google)
  render(<Page />);
  expect(screen.getByRole("heading", { level: 1, name: "Meetings" })).toBeInTheDocument();
  expect(screen.getByText("agents@salon.thelightbulb.company")).toHaveClass("vx-code");
  expect(screen.queryByText("agents@thelightbulb.company")).toBeNull();
});

it("with no company slug there is no address, so it says what to look for", () => {
  cfg.invite = undefined;
  render(<Page />);
  expect(screen.getByText(/Add your assistant's meeting address as a guest/)).toBeInTheDocument();
});

it("saves meetings.followUps on toggle", async () => {
  render(<Page />);
  fireEvent.click(screen.getByLabelText("Turn action items into follow-ups"));
  await waitFor(() => expect(screen.getByText("Saved")).toBeInTheDocument());
  expect(cfg.save).toHaveBeenCalledWith(expect.objectContaining({ name: "Nova", meetings: { followUps: false } }));
});

it("after a save the switch follows the stored config again", async () => {
  const { rerender } = render(<Page />);
  const box = screen.getByLabelText("Turn action items into follow-ups");
  fireEvent.click(box);
  await waitFor(() => expect(screen.getByText("Saved")).toBeInTheDocument());
  cfg.value = { ...cfg.value, meetings: { followUps: true } };
  rerender(<Page />);
  expect(box).toBeChecked();
  expect(screen.getByText("Saved")).toBeInTheDocument();
});

it("a second click while saving sends no second request", async () => {
  let finish!: () => void;
  cfg.save.mockReturnValueOnce(new Promise<void>((r) => { finish = r; }));
  render(<Page />);
  const box = screen.getByLabelText("Turn action items into follow-ups");
  fireEvent.click(box);
  expect(box).toBeDisabled();
  expect(box).toHaveAttribute("aria-busy", "true");
  fireEvent.click(box);
  expect(cfg.save).toHaveBeenCalledTimes(1);
  finish();
  await waitFor(() => expect(box).toBeEnabled());
  expect(screen.getByText("Saved")).toBeInTheDocument();
});

it("a member who cannot edit sees why it did not save", async () => {
  cfg.save.mockRejectedValueOnce({ status: 403 });
  render(<Page />);
  fireEvent.click(screen.getByLabelText("Turn action items into follow-ups"));
  await waitFor(() => expect(screen.getByText("You can't change the assistant from this account.")).toBeInTheDocument());
  expect(screen.queryByText("Saved")).toBeNull();
  expect(screen.getByLabelText("Turn action items into follow-ups")).toBeEnabled();
});
