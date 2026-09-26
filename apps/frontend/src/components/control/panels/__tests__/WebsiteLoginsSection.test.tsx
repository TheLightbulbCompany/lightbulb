import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = { get: vi.fn(), post: vi.fn(), del: vi.fn() };
vi.mock("@/lib/api", () => ({ useApi: () => api }));

import { WebsiteLoginsSection } from "../WebsiteLoginsSection";

const login = { id: "vault_0123456789ab", origin: "https://app.supademo.com", username: "me@example.com",
  addedBy: "user_1", addedByName: "ann@example.com", createdAt: "2026-09-25T00:00:00Z", lastUsedAt: null };

function mount(canManage = true) {
  return render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
    <WebsiteLoginsSection canManage={canManage} />
  </SWRConfig>);
}

describe("WebsiteLoginsSection", () => {
  beforeEach(() => {
    api.get.mockReset().mockResolvedValue({ logins: [login] });
    api.post.mockReset().mockResolvedValue({});
    api.del.mockReset().mockResolvedValue(null);
  });

  it("lists site, username, who added it and last use — never a password", async () => {
    mount();
    const cell = await screen.findByText("app.supademo.com");
    const tr = cell.closest("tr")!;
    expect(within(tr).getByText("me@example.com")).toBeInTheDocument();
    expect(within(tr).getByText("ann@example.com")).toBeInTheDocument();
    expect(within(tr).getByText("Never")).toBeInTheDocument();
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
    await userEvent.click(await screen.findByRole("button", { name: "Remove the login for app.supademo.com" }));
    await waitFor(() => expect(api.del).toHaveBeenCalledWith("/website-logins/vault_0123456789ab"));
  });

  it("shows non-admins why there is nothing to manage and reads nothing", () => {
    mount(false);
    expect(screen.getByText("Only organization admins can manage website logins.")).toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
  });
});
