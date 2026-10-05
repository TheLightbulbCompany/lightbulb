import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() };
vi.mock("@/lib/api", () => ({ useApi: () => api }));

import { WebsiteLoginsSection } from "../WebsiteLoginsSection";

const login = { id: "vault_0123456789ab", origin: "https://app.supademo.com", username: "me@example.com",
  addedBy: "user_1", addedByName: "ann@example.com", createdAt: "2026-09-25T00:00:00Z", lastUsedAt: null };
const sites = [
  { site: "google.com", signedIn: true, logins: [] },
  { site: "supademo.com", signedIn: false, logins: [login] },
];

function mount(canManage = true) {
  return render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
    <WebsiteLoginsSection canManage={canManage} />
  </SWRConfig>);
}

const row = async (site: string) => screen.findByRole("listitem", { name: site });

describe("WebsiteLoginsSection", () => {
  beforeEach(() => {
    api.get.mockReset().mockResolvedValue({ sites });
    api.post.mockReset().mockResolvedValue(null);
    api.patch.mockReset().mockResolvedValue({});
    api.del.mockReset().mockResolvedValue(null);
  });

  it("shows one row per site: its saved usernames and whether the browser is signed in", async () => {
    mount();
    expect(screen.getByRole("heading", { name: "Website sign-ins" })).toBeInTheDocument();
    const supademo = await row("supademo.com");
    expect(api.get).toHaveBeenCalledWith("/website-logins/sites");
    expect(within(supademo).getByText("me@example.com")).toBeInTheDocument();
    expect(within(supademo).getByText(/app\.supademo\.com · Added by ann@example\.com · Never used/)).toBeInTheDocument();
    expect(within(supademo).getByText("Not signed in in the browser")).toBeInTheDocument();
    expect(within(supademo).queryByRole("button", { name: /Sign out/ })).not.toBeInTheDocument();
    const google = await row("google.com");
    expect(within(google).getByText("Signed in in the browser")).toBeInTheDocument();
    expect(within(google).getByText("No saved login. Signed in by hand in the browser.")).toBeInTheDocument();
  });

  it("signs the browser out of a site only after the confirmation", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    mount();
    const button = within(await row("google.com")).getByRole("button", { name: "Sign out of google.com" });
    await userEvent.click(button);
    expect(confirm).toHaveBeenCalledWith("Agents will be signed out of google.com in the workspace browser.");
    expect(api.post).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    await userEvent.click(button);
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/website-logins/sites/sign-out", { site: "google.com" }));
  });

  it("says why a sign-out could not happen", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    api.post.mockRejectedValue(new Error("An agent is using the browser. Try again in a moment."));
    mount();
    await userEvent.click(within(await row("google.com")).getByRole("button", { name: "Sign out of google.com" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("An agent is using the browser. Try again in a moment.");
  });

  it("fixes a username without touching the password", async () => {
    mount();
    await userEvent.click(within(await row("supademo.com")).getByRole("button", { name: "Edit the username me@example.com" }));
    const input = screen.getByLabelText("Username or email");
    expect(input).toHaveValue("me@example.com");
    expect(screen.getByRole("button", { name: "Save username" })).toBeDisabled(); // unchanged
    await userEvent.clear(input);
    await userEvent.type(input, "you@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Save username" }));
    expect(api.patch).toHaveBeenCalledWith("/website-logins/vault_0123456789ab", { username: "you@example.com" });
  });

  it("replaces a password through a masked, empty field", async () => {
    mount();
    await userEvent.click(within(await row("supademo.com")).getByRole("button", { name: "Replace the password for me@example.com" }));
    const input = screen.getByLabelText("New password for me@example.com");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveValue("");
    await userEvent.type(input, "n3w");
    await userEvent.click(screen.getByRole("button", { name: "Save password" }));
    expect(api.patch).toHaveBeenCalledWith("/website-logins/vault_0123456789ab", { password: "n3w" });
  });

  it("adds a login through a masked field", async () => {
    mount();
    await userEvent.click(await screen.findByRole("button", { name: "Add login" }));
    await userEvent.type(screen.getByLabelText("Sign-in page address"), "supademo.com");
    await userEvent.type(screen.getByLabelText("Username or email"), "u@x.com");
    const password = screen.getByLabelText("Password");
    expect(password).toHaveAttribute("type", "password");
    await userEvent.type(password, "pw");
    await userEvent.click(screen.getByRole("button", { name: "Save login" }));
    expect(api.post).toHaveBeenCalledWith("/website-logins", { origin: "supademo.com", username: "u@x.com", password: "pw" });
  });

  it("removes a login after confirming", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount();
    await userEvent.click(within(await row("supademo.com")).getByRole("button", { name: "Remove me@example.com for app.supademo.com" }));
    await waitFor(() => expect(api.del).toHaveBeenCalledWith("/website-logins/vault_0123456789ab"));
  });

  it("shows non-admins why there is nothing to manage and reads nothing", () => {
    mount(false);
    expect(screen.getByText("Only organization admins can manage website sign-ins.")).toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
  });
});
