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
    fireEvent.click(screen.getByRole("button", { name: "Change who can open it" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("a1", { open_to: "invited" }));
    fireEvent.change(await screen.findByLabelText("Email addresses"), { target: { value: "a@x.com, b@x.com\nc@x.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Invite" }));
    await waitFor(() => expect(invite).toHaveBeenCalledWith("a1", ["a@x.com", "b@x.com", "c@x.com"]));
  });

  it("widening applies on one click", async () => {
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("Everyone in the workspace"));
    await waitFor(() => expect(update).toHaveBeenCalledWith("a1", { open_to: "workspace" }));
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("narrowing asks first and names who loses access", async () => {
    render(<AppSettings app={app({ open_to: "workspace" })} onClose={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("This room"));
    expect(screen.getByText("Everyone in the workspace outside #finance will lose access.")).toBeTruthy();
    expect(update).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Change who can open it" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("a1", { open_to: "room" }));
  });

  it("choosing people I invite asks first", () => {
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("People I invite"));
    expect(screen.getByText(/People in #finance will lose access unless you invite them/)).toBeTruthy();
    expect(update).not.toHaveBeenCalled();
  });

  it("cancel keeps the current audience and sends nothing", () => {
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("People I invite"));
    expect((screen.getByLabelText("People I invite") as HTMLInputElement).checked).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect((screen.getByLabelText("This room") as HTMLInputElement).checked).toBe(true);
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    expect(update).not.toHaveBeenCalled();
  });

  it("invited to workspace names the outside invitees", () => {
    render(<AppSettings app={app({ open_to: "invited" })} onClose={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("Everyone in the workspace"));
    expect(screen.getByText("People you invited from outside will lose access.")).toBeTruthy();
    expect(update).not.toHaveBeenCalled();
  });

  it("picking the current audience while confirming sends nothing", () => {
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("People I invite"));
    fireEvent.click(screen.getByLabelText("This room"));
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    expect((screen.getByLabelText("This room") as HTMLInputElement).checked).toBe(true);
    expect(update).not.toHaveBeenCalled();
  });

  const labels = { room: "This room", workspace: "Everyone in the workspace", invited: "People I invite", public: "Anyone with the link" } as const;
  const widening = ["room>workspace", "room>public", "workspace>public", "invited>public"];
  const modes = Object.keys(labels) as (keyof typeof labels)[];
  const pairs = modes.flatMap((f) => modes.filter((t) => t !== f).map((t) => [f, t] as const));
  it.each(pairs)("%s to %s", async (from, to) => {
    render(<AppSettings app={app({ open_to: from })} onClose={vi.fn()} />);
    fireEvent.click(screen.getByLabelText(labels[to]));
    if (widening.includes(`${from}>${to}`)) {
      await waitFor(() => expect(update).toHaveBeenCalledWith("a1", { open_to: to }));
    } else {
      expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
      expect(update).not.toHaveBeenCalled();
    }
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
