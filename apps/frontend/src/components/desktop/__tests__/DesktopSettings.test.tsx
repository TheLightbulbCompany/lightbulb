// @vitest-environment jsdom
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { DesktopSettings } from "../DesktopSettings";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ userId: "alice", orgId: "org_3H3vi", getToken: vi.fn() }),
  useOrganization: () => ({ organization: { id: "org_3H3vi", name: "Acme" } }),
}));

const api = vi.hoisted(() => {
  const prefs = { sessionsReachableFromBackgroundWork: false };
  return { get: vi.fn(async () => prefs), put: vi.fn(async (_path: string, body: object) => ({ ...prefs, ...body })) };
});
vi.mock("@/lib/api", () => ({ useApi: () => api }));

afterEach(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  delete (window as any).__TAURI__;
});

it("in a browser shows only the account-level session switches, no download", async () => {
  render(<DesktopSettings />);
  expect(screen.queryByRole("link")).toBeNull();
  expect(screen.queryByRole("heading", { level: 2 })).toBeNull();
  const background = await screen.findByRole("switch", { name: /Background work/ });
  expect(background).not.toBeChecked();
  expect(screen.getAllByRole("switch")).toHaveLength(1);
  await userEvent.click(background);
  expect(api.put).toHaveBeenCalledWith("/users/me/prefs", { sessionsReachableFromBackgroundWork: true });
  await waitFor(() => expect(background).toBeChecked());
});

it("in the desktop app shows the four cards in order and no raw org id", async () => {
  const invoke = vi.fn(async (cmd: string) => {
    switch (cmd) {
      case "plugin:autostart|is_enabled": return true;
      case "desktop_permissions_get": return { consented: true, shell: true, browser: false, files: true, screen: false, paperclip: false, updatedAt: "1" };
      case "file_access_list_paths": return [];
      case "local_sessions_status": return { enrollment: { ownerId: "org_3H3vi", humanUserId: "alice", enabled: true, paused: false }, pairedWorkspaceId: "org_3H3vi", status: "" };
      case "shell_integration_status": return false;
      default: return undefined;
    }
  });
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).__TAURI__ = { core: { invoke } };
  render(<DesktopSettings />);
  expect(await screen.findByRole("switch", { name: "Open at login" })).toBeChecked();
  expect(await screen.findByText("On · Acme")).toBeInTheDocument();
  expect(await screen.findByText("No folders yet.")).toBeInTheDocument();
  expect(await screen.findByRole("switch", { name: "Share every new session" })).not.toBeChecked();
  // Shown once, after Session sharing, not inside it.
  expect(await screen.findAllByRole("switch", { name: /Background work/ })).toHaveLength(1);
  const order = ["Open at login", "Agents on this Mac", "Approved folders", "Session sharing"].map((text) => screen.getByText(text));
  for (let i = 1; i < order.length; i++) {
    expect(order[i - 1].compareDocumentPosition(order[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  }
  expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual(["Agents on this Mac", "Approved folders", "Session sharing"]);
  expect(document.body.textContent).not.toMatch(/org_/);
});
