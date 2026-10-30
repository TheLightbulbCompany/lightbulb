import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AppRow } from "@/hooks/useApps";

const update = vi.fn();
const invite = vi.fn();
const remove = vi.fn();
const databaseLink = vi.fn();
let admin = true;
vi.mock("@/hooks/useApps", () => ({ useApps: () => ({ update, invite, remove, databaseLink }) }));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => admin }));
import { AppSettings } from "../AppSettings";

const neon = { provider: "neon", service: "db", state: "ready", resource_id: "n1", environment: "prod" };
const app = (over: Partial<AppRow> = {}): AppRow => ({
  app_id: "a1", name: "Invoices", room_id: "r1", room_name: "finance", state: "ready", open_to: "room",
  budget_usd: 20, preview_url: null, live_url: "https://x", live_version: 3, last_opened_at: null,
  spend_month_cents: 0, can_open: true, resources: [neon], ...over,
});

beforeEach(() => {
  [update, invite, remove, databaseLink].forEach((f) => f.mockReset());
  update.mockResolvedValue({}); invite.mockResolvedValue(undefined); remove.mockResolvedValue(undefined);
  admin = true;
});

describe("AppSettings", () => {
  it("choosing people I invite shows the email list", async () => {
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    expect(screen.queryByLabelText("Email addresses")).toBeNull();
    fireEvent.click(screen.getByLabelText("People I invite"));
    await waitFor(() => expect(update).toHaveBeenCalledWith("a1", { open_to: "invited" }));
    fireEvent.change(await screen.findByLabelText("Email addresses"), { target: { value: "a@x.com, b@x.com\nc@x.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Invite" }));
    await waitFor(() => expect(invite).toHaveBeenCalledWith("a1", ["a@x.com", "b@x.com", "c@x.com"]));
  });

  it("data and backups opens the provider console link", async () => {
    databaseLink.mockResolvedValue("https://console.neon.tech/x");
    const tab = { location: { href: "" }, close: vi.fn(), opener: {} };
    const open = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Data and backups" }));
    await waitFor(() => expect(tab.location.href).toBe("https://console.neon.tech/x"));
    open.mockRestore();
  });

  it("remove needs the typed name", async () => {
    const onClose = vi.fn();
    render(<AppSettings app={app()} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "Remove app…" }));
    const confirm = screen.getByRole("button", { name: "Remove Invoices" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Type the app name to confirm"), { target: { value: "invoices" } });
    expect(confirm.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Type the app name to confirm"), { target: { value: "Invoices" } });
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    await waitFor(() => expect(remove).toHaveBeenCalledWith("a1", "Invoices"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("saves the monthly budget in whole dollars", async () => {
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Monthly budget (USD)"), { target: { value: "35" } });
    fireEvent.click(screen.getByRole("button", { name: "Save budget" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("a1", { budget_usd: 35 }));
  });

  it("members never see the settings", () => {
    admin = false;
    const { container } = render(<AppSettings app={app()} onClose={vi.fn()} />);
    expect(container.innerHTML).toBe("");
  });
});
