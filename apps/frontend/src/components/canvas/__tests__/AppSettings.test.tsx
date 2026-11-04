import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { AppRow } from "@/hooks/useApps";

const update = vi.fn();
const invite = vi.fn();
const remove = vi.fn();
const listInvites = vi.fn();
const revokeInvite = vi.fn();
const databaseLink = vi.fn();
const publish = vi.fn();
const refresh = vi.fn();
let admin = true;
vi.mock("@/hooks/useApps", () => ({ useApps: () => ({ update, invite, listInvites, revokeInvite, remove, databaseLink, publish, refresh }) }));
const notifySuccess = vi.fn();
vi.mock("@/lib/notify", () => ({ notifySuccess: (m: string) => notifySuccess(m), notifyError: vi.fn() }));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => admin }));
import { AppSettings } from "../AppSettings";

const neon = { provider: "Neon", service: "db", state: "ready", resource_id: "n1", environment: "prod" };
const app = (over: Partial<AppRow> = {}): AppRow => ({
  app_id: "a1", name: "Invoices", room_id: "r1", room_name: "finance", state: "ready", open_to: "room",
  budget_usd: 20, preview_url: null, live_url: "https://x", live_version: 3, last_opened_at: null,
  spend_month_cents: 0, can_open: true, resources: [neon], ...over,
});

beforeEach(() => {
  [update, invite, remove, databaseLink, listInvites, revokeInvite].forEach((f) => f.mockReset());
  listInvites.mockResolvedValue([]); revokeInvite.mockResolvedValue(undefined);
  update.mockResolvedValue({}); invite.mockResolvedValue(undefined); remove.mockResolvedValue(undefined);
  admin = true;
});

describe("AppSettings", () => {
  it("offers Only me, and no room choice for an app that came from no room", async () => {
    const { unmount } = render(<AppSettings app={app({ is_creator: true })} onClose={vi.fn()} />);
    expect(screen.getByLabelText("#finance (this room)")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Only me (and workspace admins)"));
    expect(screen.getByText("Only you and workspace admins will be able to open it.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Change who can open it" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("a1", { open_to: "me" }));
    unmount();
    render(<AppSettings app={app({ room_id: null, room_name: undefined, open_to: "workspace", is_creator: true })} onClose={vi.fn()} />);
    expect(screen.queryByLabelText(/this room/)).toBeNull();
    expect(screen.getByLabelText("Only me (and workspace admins)")).toBeTruthy();
  });

  it("an admin who didn't make the app is never told it's theirs", () => {
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    expect(screen.queryByLabelText("Only me (and workspace admins)")).toBeNull();
    fireEvent.click(screen.getByLabelText("Only its creator and workspace admins"));
    expect(screen.getByText("Only its creator and workspace admins will be able to open it.")).toBeTruthy();
  });

  it("lists invitees on a room app too: invite links work on any app that isn't public", async () => {
    listInvites.mockResolvedValue(["ann@x.com"]);
    render(<AppSettings app={app({ open_to: "room" })} onClose={vi.fn()} />);
    expect(await screen.findByRole("button", { name: "Remove ann@x.com" })).toBeTruthy();
  });

  it("choosing people I invite shows the email list", async () => {
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    expect(screen.queryByLabelText(/Email addresses/)).toBeNull();
    fireEvent.click(screen.getByLabelText("People I invite"));
    fireEvent.click(screen.getByRole("button", { name: "Change who can open it" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("a1", { open_to: "invited" }));
    fireEvent.change(await screen.findByLabelText(/Email addresses/), { target: { value: "a@x.com, b@x.com\nc@x.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Invite" }));
    expect(screen.getByText("Each person gets an email with a link to Invoices only. No Lightbulb account needed.")).toBeTruthy();
    expect(screen.getByLabelText("Email addresses (one per line or separated by commas)")).toBeTruthy();
    await waitFor(() => expect(invite).toHaveBeenCalledWith("a1", ["a@x.com", "b@x.com", "c@x.com"]));
  });

  it("says how many invites were sent", async () => {
    render(<AppSettings app={app({ open_to: "invited" })} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/Email addresses/), { target: { value: "a@x.com b@x.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Invite" }));
    expect((await screen.findByRole("status")).textContent).toBe("Sent 2 invites.");
  });

  it("widening applies on one click", async () => {
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("Everyone in the workspace"));
    await waitFor(() => expect(update).toHaveBeenCalledWith("a1", { open_to: "workspace" }));
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("narrowing asks first and names who loses access", async () => {
    render(<AppSettings app={app({ open_to: "workspace" })} onClose={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("#finance (this room)"));
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
    expect((screen.getByLabelText("#finance (this room)") as HTMLInputElement).checked).toBe(true);
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
    fireEvent.click(screen.getByLabelText("#finance (this room)"));
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    expect((screen.getByLabelText("#finance (this room)") as HTMLInputElement).checked).toBe(true);
    expect(update).not.toHaveBeenCalled();
  });

  const labels = { room: "#finance (this room)", workspace: "Everyone in the workspace", invited: "People I invite", public: "Anyone with the link" } as const;
  const widening = ["room>workspace"];
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
    fireEvent.click(screen.getByRole("button", { name: "Data and backups ↗" }));
    await waitFor(() => expect(tab.location.href).toBe("https://console.neon.tech/x"));
    expect(screen.getByText("Opens where Invoices's data is kept (Neon). Restoring a backup replaces today's data.")).toBeTruthy();
    open.mockRestore();
  });

  it("a data link failure says what to do, raw text under Details", async () => {
    databaseLink.mockRejectedValue(new Error("neon 500"));
    const tab = { location: { href: "" }, close: vi.fn(), opener: {} };
    const open = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Data and backups ↗" }));
    expect((await screen.findByRole("alert")).textContent).toContain("We couldn't open the data page. Try again.");
    expect(screen.getByText("Details").closest("details")!.textContent).toContain("neon 500");
    open.mockRestore();
  });

  it("a null data link closes the tab and hides the button, keeping the Neon text", async () => {
    databaseLink.mockResolvedValue(null);
    const tab = { location: { href: "" }, close: vi.fn(), opener: {} };
    const open = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Data and backups ↗" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Data and backups ↗" })).toBeNull());
    expect(tab.close).toHaveBeenCalled();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("Invoices's data is kept in Neon.")).toBeTruthy();
    open.mockRestore();
  });

  it("null spend says not yet known, never $0 or NaN", () => {
    render(<AppSettings app={app({ spend_month_cents: null })} onClose={vi.fn()} />);
    expect(screen.getByText("What Invoices may spend each month on hosting and data. Spent so far: not yet known.")).toBeTruthy();
  });

  it("going public asks one confirmation, then shows the live link with Copy link", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const { rerender } = render(<AppSettings app={app()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("Anyone with the link"));
    expect(screen.getByText(/^Anyone with the link will be able to open it/)).toBeTruthy();
    expect(update).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Make it public" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("a1", { open_to: "public" }));
    expect(screen.getByRole("button", { name: "Copy link" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Copy link" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("https://x"));
    rerender(<AppSettings app={app({ open_to: "public", live_url: null })} onClose={vi.fn()} />);
    expect(screen.getByText("Publish it to get the link.")).toBeTruthy();
  });

  it("cancel on the public confirm sends nothing", () => {
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByLabelText("Anyone with the link"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(update).not.toHaveBeenCalled();
  });

  it("a failed copy says so", async () => {
    Object.defineProperty(navigator, "clipboard", { value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) }, configurable: true });
    render(<AppSettings app={app({ open_to: "public" })} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Copy link" }));
    expect((await screen.findByRole("status")).textContent).toContain("Couldn't copy the link");
  });

  it("the creator (can_publish) sees settings without being an admin", () => {
    admin = false;
    render(<AppSettings app={app({ can_publish: true })} onClose={vi.fn()} />);
    expect(screen.getByText("Settings")).toBeTruthy();
  });

  it("budget says what it is and what was spent, with no at-limit sentence", () => {
    render(<AppSettings app={app({ spend_month_cents: 410 })} onClose={vi.fn()} />);
    expect(screen.getByText("What Invoices may spend each month on hosting and data. Spent so far: $4.10.")).toBeTruthy();
  });

  it("remove needs the typed display name, sends it, and toasts", async () => {
    const onClose = vi.fn();
    render(<AppSettings app={app({ name: "workshop-signup" })} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "Remove app…" }));
    expect(screen.getByText("Removing Workshop Signup takes it offline for everyone and deletes its data. This can't be undone.")).toBeTruthy();
    const confirm = screen.getByRole("button", { name: "Remove Workshop Signup" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    const box = screen.getByLabelText("Type Workshop Signup to confirm");
    fireEvent.change(box, { target: { value: "workshop-signup" } });
    expect(confirm.disabled).toBe(true);
    fireEvent.change(box, { target: { value: "  workshop signup " } });
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    await waitFor(() => expect(remove).toHaveBeenCalledWith("a1", "Workshop Signup"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(notifySuccess).toHaveBeenCalledWith("Removed Workshop Signup.");
  });

  it("remove can be cancelled", () => {
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Remove app…" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Remove app…" })).toBeTruthy();
  });

  it("saves the monthly budget in whole dollars", async () => {
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Monthly spending limit ($)"), { target: { value: "35" } });
    fireEvent.click(screen.getByRole("button", { name: "Save limit" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("a1", { budget_usd: 35 }));
  });

  it("members never see the settings", () => {
    admin = false;
    const { container } = render(<AppSettings app={app()} onClose={vi.fn()} />);
    expect(container.innerHTML).toBe("");
  });

  it("publish is offered in settings too", async () => {
    publish.mockResolvedValue({ app_id: "a1", state: "building" });
    const { unmount } = render(<AppSettings app={app({ can_publish: false, preview_url: "https://p" })} onClose={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Publish" })).toBeNull();
    unmount();
    render(<AppSettings app={app({ can_publish: true, preview_url: "https://p" })} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(screen.getByText("Make this preview live for #finance? You can undo it from its conversation.")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Publish" })[0]);
    await waitFor(() => expect(publish).toHaveBeenCalledWith("a1"));
  });

  describe("invited people", () => {
    const rows = () => within(screen.getByRole("list", { name: "Invited people" })).getAllByRole("listitem").map((r) => r.textContent);

    it("lists each person with a Remove, whichever way the app is open", async () => {
      listInvites.mockResolvedValue(["a@x.com", "b@x.com"]);
      render(<AppSettings app={app({ open_to: "workspace" })} onClose={vi.fn()} />);
      await screen.findByText("a@x.com");
      expect(listInvites).toHaveBeenCalledWith("a1");
      expect(rows()).toEqual(["a@x.com · Remove", "b@x.com · Remove"]);
    });

    it("nobody invited shows no list", async () => {
      render(<AppSettings app={app({ open_to: "invited" })} onClose={vi.fn()} />);
      await waitFor(() => expect(listInvites).toHaveBeenCalled());
      expect(screen.queryByRole("list", { name: "Invited people" })).toBeNull();
    });

    it("Remove takes the row away and says the link no longer works", async () => {
      listInvites.mockResolvedValue(["a@x.com", "b@x.com"]);
      render(<AppSettings app={app({ open_to: "invited" })} onClose={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Remove a@x.com" }));
      expect((await screen.findByRole("status")).textContent).toBe("a@x.com's invite link no longer works. If they already opened it, they keep access for up to a day.");
      expect(revokeInvite).toHaveBeenCalledWith("a1", "a@x.com");
      expect(rows()).toEqual(["b@x.com · Remove"]);
    });

    it("a failed Remove keeps the row", async () => {
      listInvites.mockResolvedValue(["a@x.com"]);
      revokeInvite.mockRejectedValue(new Error("nope"));
      render(<AppSettings app={app({ open_to: "invited" })} onClose={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Remove a@x.com" }));
      await waitFor(() => expect(revokeInvite).toHaveBeenCalled());
      expect(rows()).toEqual(["a@x.com · Remove"]);
      expect(screen.queryByRole("status")).toBeNull();
    });

    it("a failed load says so and can be tried again", async () => {
      listInvites.mockRejectedValueOnce(new Error("down")).mockResolvedValueOnce(["a@x.com"]);
      render(<AppSettings app={app()} onClose={vi.fn()} />);
      expect((await screen.findByRole("alert")).textContent).toContain("We couldn't load who's invited.");
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      await screen.findByText("a@x.com");
      expect(screen.queryByRole("alert")).toBeNull();
    });

    it("sending invites refreshes the list", async () => {
      render(<AppSettings app={app({ open_to: "invited" })} onClose={vi.fn()} />);
      listInvites.mockResolvedValue(["new@x.com"]);
      fireEvent.change(screen.getByLabelText(/Email addresses/), { target: { value: "new@x.com" } });
      fireEvent.click(screen.getByRole("button", { name: "Invite" }));
      await screen.findByText("new@x.com");
    });

    it("someone who can't manage the app never asks for the list", async () => {
      admin = false;
      const { container } = render(<AppSettings app={app({ can_publish: false })} onClose={vi.fn()} />);
      expect(container.innerHTML).toBe("");
      expect(listInvites).not.toHaveBeenCalled();
    });
  });

  it("shows own domain as coming soon, right after Anyone with the link", async () => {
    render(<AppSettings app={app()} onClose={vi.fn()} />);
    await waitFor(() => expect(listInvites).toHaveBeenCalled());
    const row = screen.getByRole("button", { name: "Use your own domain (coming soon)" }) as HTMLButtonElement;
    expect(row.disabled).toBe(true);
    const anyone = screen.getByLabelText("Anyone with the link").closest("label")!;
    expect(anyone.nextElementSibling?.contains(row)).toBe(true);
  });

  it("never shows the short web-address name outside the address", async () => {
    const { container } = render(<AppSettings app={app({ name: "workshop-signup", display_name: "Workshop Sign-up" })} onClose={vi.fn()} />);
    await waitFor(() => expect(listInvites).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Remove app…" }));
    expect(screen.getByLabelText("Type Workshop Sign-up to confirm")).toBeTruthy();
    expect(container.textContent).not.toContain("workshop-signup");
  });
});
