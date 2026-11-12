// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import NetworkAppsPage from "../page";

const s = vi.hoisted(() => ({
  signedIn: true,
  wakes: [] as unknown[],
  get: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
  post: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: s.get, put: s.put, del: s.del, post: s.post }) }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: s.signedIn }),
  SignIn: () => <div>sign in</div>,
}));
vi.mock("../AgentKeysSection", () => ({ AgentKeysSection: () => <div>agent keys</div> }));

function page() {
  return render(<SWRConfig value={{ provider: () => new Map() }}><NetworkAppsPage /></SWRConfig>);
}

beforeEach(() => {
  vi.clearAllMocks();
  s.signedIn = true;
  s.get.mockImplementation(async (url: string) =>
    url === "/network/apps"
      ? { apps: [{ clientId: "c1", client: "ChatGPT", route: { kind: "computer", key: "k1", name: "mac" } }],
          routes: [{ kind: "computer", key: "k1", name: "mac" }, { kind: "computer", key: "k2", name: "mbp" }] }
      : { wakes: s.wakes });
});

describe("your apps on the network", () => {
  it("an app shows its pinned computer and moves to another", async () => {
    page();
    const select = await screen.findByLabelText("Computer for ChatGPT");
    expect((select as HTMLSelectElement).value).toBe("k1");
    fireEvent.change(select, { target: { value: "k2" } });
    await waitFor(() => expect(s.put).toHaveBeenCalledWith("/network/apps/route", { clientId: "c1", key: "k2" }));
  });

  it("signs out one of your connected computers", async () => {
    page();
    const buttons = await screen.findAllByRole("button", { name: "Sign out" });
    expect(buttons).toHaveLength(2);
    fireEvent.click(buttons[1]);
    await waitFor(() => expect(s.post).toHaveBeenCalledWith("/network/computers/k2/sign-out", {}));
  });

  it("signed out shows sign-in", () => {
    s.signedIn = false;
    page();
    expect(screen.getByText("sign in")).toBeInTheDocument();
  });

  it("a waiting wake-up takes its key here, and agent keys are offered", async () => {
    s.wakes = [{ app: "agent-k1", client: "Grok Bot", host: "hooks.example", state: "needs_key" }];
    page();
    expect(await screen.findByText("Grok Bot")).toBeInTheDocument();
    expect(screen.getByText(/Waiting for its key/)).toBeInTheDocument();
    expect(screen.getByText("agent keys")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Webhook key for Grok Bot"), { target: { value: " whk " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(s.put).toHaveBeenCalledWith("/network/wakes/agent-k1", { key: "whk" }));
  });
});
