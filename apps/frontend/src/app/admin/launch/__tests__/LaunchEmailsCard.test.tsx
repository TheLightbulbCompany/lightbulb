import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
const saveLaunchEmailCopy = vi.fn();
const sendLaunchTestEmail = vi.fn();
vi.mock("../../_actions/launch", () => ({
  saveLaunchEmailCopy: (body: unknown) => saveLaunchEmailCopy(body),
  sendLaunchTestEmail: (variant: string) => sendLaunchTestEmail(variant),
}));

import type { LaunchEmailVariant } from "../../_lib/api";
import { LaunchEmailsCard } from "../LaunchEmailsCard";

const variant = (over: Partial<LaunchEmailVariant> = {}): LaunchEmailVariant => ({
  variant: "competing",
  placeholders: ["comp_slots"],
  defaults: {
    subject: "You're in the launch competition",
    lead: "The sign-up sheet is closed, and you're in the competition.",
    detail: "The {comp_slots} free months went to the earliest spots.",
  },
  override: {},
  preview_subject: "You're in the launch competition",
  preview_html:
    '<!doctype html><html><head><meta charset="utf-8"></head>' +
    '<body style="margin:0;padding:16px;background:#ffffff"><div><p>preview body</p></div></body></html>',
  ...over,
});

beforeEach(() => {
  saveLaunchEmailCopy.mockReset();
  saveLaunchEmailCopy.mockResolvedValue({ ok: true, status: 200, data: {} });
  sendLaunchTestEmail.mockReset();
  sendLaunchTestEmail.mockResolvedValue({ ok: true, status: 200, data: { sent_to: "admin@x.com" } });
});

describe("LaunchEmailsCard", () => {
  it("prefills each field with the effective copy — the override where there is one", () => {
    render(<LaunchEmailsCard variants={[variant({ override: { lead: "hand-written lead" } })]} />);
    expect(screen.getByDisplayValue("hand-written lead")).toBeInTheDocument();
    // The un-overridden fields still show the shipped default.
    expect(screen.getByDisplayValue("You're in the launch competition")).toBeInTheDocument();
    expect(screen.getByText(/· edited/)).toBeInTheDocument();
  });

  it("marks an un-overridden variant as default", () => {
    render(<LaunchEmailsCard variants={[variant()]} />);
    expect(screen.getByText(/· default/)).toBeInTheDocument();
  });

  it("saves the edited copy for that variant only", async () => {
    const user = userEvent.setup();
    render(<LaunchEmailsCard variants={[variant()]} />);
    const lead = screen.getByDisplayValue("The sign-up sheet is closed, and you're in the competition.");
    await user.clear(lead);
    await user.type(lead, "New lead.");
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    expect(saveLaunchEmailCopy).toHaveBeenCalledWith({
      variant: "competing",
      subject: "You're in the launch competition",
      lead: "New lead.",
      detail: "The {comp_slots} free months went to the earliest spots.",
    });
  });

  it("reset sends BLANK fields — the row stops overriding and the shipped copy is back", async () => {
    const user = userEvent.setup();
    render(<LaunchEmailsCard variants={[variant({ override: { lead: "hand-written" } })]} />);
    await user.click(screen.getByRole("button", { name: /reset to default/i }));
    expect(saveLaunchEmailCopy).toHaveBeenCalledWith({ variant: "competing", subject: "", lead: "", detail: "" });
    expect(screen.getByDisplayValue("The sign-up sheet is closed, and you're in the competition.")).toBeInTheDocument();
  });

  it("surfaces a 422 field error from an unsupported placeholder", async () => {
    const user = userEvent.setup();
    saveLaunchEmailCopy.mockResolvedValue({
      ok: false,
      status: 422,
      data: { detail: { errors: { lead: "unsupported placeholder(s): {referral_link}" } } },
    });
    render(<LaunchEmailsCard variants={[variant()]} />);
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    expect(await screen.findByText(/lead: unsupported placeholder\(s\): \{referral_link\}/)).toBeInTheDocument();
  });

  it("sends a test to the signed-in admin and says where it went", async () => {
    const user = userEvent.setup();
    render(<LaunchEmailsCard variants={[variant()]} />);
    await user.click(screen.getByRole("button", { name: /send test to me/i }));
    expect(sendLaunchTestEmail).toHaveBeenCalledWith("competing");
    expect(await screen.findByText(/Test sent to admin@x.com\./)).toBeInTheDocument();
  });

  it("renders the preview in a sandboxed iframe, never as live markup", () => {
    const { container } = render(<LaunchEmailsCard variants={[variant()]} />);
    const frame = container.querySelector("iframe");
    expect(frame).not.toBeNull();
    expect(frame).toHaveAttribute("sandbox", "");
    // The light ground rides INSIDE the document the backend built, not on the
    // iframe element — a Tailwind palette literal here is banned, and a theme
    // token would put the email's hardcoded dark text on a dark ground.
    expect(frame!.getAttribute("srcdoc")).toContain("background:#ffffff");
    expect(frame!.className).not.toMatch(/\bbg-/);
    expect(screen.queryByText("preview body")).not.toBeInTheDocument();
  });

  it("lists the placeholders the variant supports", () => {
    render(<LaunchEmailsCard variants={[variant()]} />);
    const block = screen.getByText(/Plain text/).closest("p") as HTMLElement;
    expect(within(block).getByText(/Placeholders: \{comp_slots\}\./)).toBeInTheDocument();
  });
});
