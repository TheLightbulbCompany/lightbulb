import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import * as WebBrowser from "expo-web-browser";
import { AppState, Linking, Share } from "react-native";

jest.mock("expo-web-browser", () => ({ openAuthSessionAsync: jest.fn() }));

const mockPush = jest.fn();
jest.mock("expo-router", () => {
  const { useEffect } = require("react");
  return {
    router: { push: (...args: unknown[]) => mockPush(...args) },
    useFocusEffect: (cb: () => void) => useEffect(cb, [cb]),
  };
});

let mockOrg: { id: string; membersCount: number } | null = { id: "org_1", membersCount: 1 };
let mockRole = "org:admin";
jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ getToken: async () => "token" }),
  useOrganization: () => ({ organization: mockOrg, membership: { role: mockRole } }),
}));

const mockOpenBrowser = jest.fn(async () => {});
let appStateChanged: ((state: string) => void) | undefined;
let urlChanged: ((event: { url: string }) => void) | undefined;
jest.mock("expo-crypto", () => ({ randomUUID: () => "33333333-3333-4333-8333-333333333333" }));

// path -> response (a function to throw / hang). Anything unlisted rejects.
let mockGets: Record<string, unknown> = {};
const mockGet = jest.fn(async (path: string) => {
  const answer = mockGets[path];
  if (typeof answer === "function") return (answer as () => unknown)();
  if (answer === undefined) throw new Error(`unexpected GET ${path}`);
  return answer;
});
const mockPost = jest.fn(async () => ({ join_url: "https://thelightbulb.company/join/new" }));
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({ get: mockGet, post: mockPost }),
}));

import { dismissKey, OnboardingBand } from "../OnboardingBand";

const participants = [
  { kind: "human", id: "user_1", displayName: "Alice" },
  { kind: "agent", id: "agent_ceo", displayName: "CEO" },
] as const;
const catalog = {
  connections: [
    { provider: "notion", status: "disconnected" },
    { provider: "github", status: "disconnected" },
    { provider: "stripe", status: "disconnected" },
    { provider: "linear", status: "disconnected" },
  ],
};
const PAID_ON_STARTER = { is_subscribed: true, plan: "standard", provider_choice: null };

function setReads(over: Record<string, unknown> = {}) {
  mockGets = {
    "/connections": catalog,
    "/teams/agents": { agents: [{ id: "agent_ceo", name: "CEO" }] },
    "/billing/account": PAID_ON_STARTER,
    ...over,
  };
}

async function renderBand() {
  await render(<OnboardingBand participants={participants} />);
  await act(async () => { await Promise.resolve(); });
}

let orgSeq = 0;
beforeEach(() => {
  orgSeq += 1;
  mockOrg = { id: `org_${orgSeq}`, membersCount: 1 };
  mockRole = "org:admin";
  mockPush.mockClear();
  mockGet.mockClear();
  mockPost.mockClear();
  mockOpenBrowser.mockReset();
  (WebBrowser.openAuthSessionAsync as jest.Mock).mockReset();
  jest.spyOn(Linking, "openURL").mockImplementation(mockOpenBrowser);
  jest.spyOn(AppState, "addEventListener").mockImplementation(((_event: string, listener: (state: string) => void) => {
    appStateChanged = listener;
    return { remove: jest.fn() };
  }) as typeof AppState.addEventListener);
  jest.spyOn(Linking, "addEventListener").mockImplementation(((_event: string, listener: (event: { url: string }) => void) => {
    urlChanged = listener;
    return { remove: jest.fn() };
  }) as unknown as typeof Linking.addEventListener);
  setReads();
});

describe("OnboardingBand", () => {
  it("renders nothing while the reads are in flight", async () => {
    setReads({ "/connections": () => new Promise(() => {}) });
    await renderBand();
    expect(screen.queryByText("Get started")).toBeNull();
  });

  it("renders nothing when /connections fails", async () => {
    setReads({ "/connections": () => { throw new Error("boom"); } });
    await renderBand();
    expect(screen.queryByText("Get started")).toBeNull();
  });

  it("renders nothing when every step is done", async () => {
    mockOrg = { id: "org_done", membersCount: 3 };
    setReads({
      "/connections": { connections: ["stripe", "github", "notion"].map((provider) => ({ provider, status: "connected" })) },
      "/teams/agents": { agents: [{ id: "agent_ceo", name: "Ada" }] },
      "/billing/account": { is_subscribed: true, plan: "standard", provider_choice: "chatgpt_oauth" },
    });
    await renderBand();
    expect(screen.queryByText("Get started")).toBeNull();
  });

  it("orders starter model, invite, connectors, then naming", async () => {
    await renderBand();
    await waitFor(() => expect(screen.getByText("Get started")).toBeTruthy());
    const labels = screen.getAllByRole("button").map((node) => node.props.accessibilityLabel as string);
    const order = [
      "You're on the starter model", "Invite your team", "Stripe", "GitHub", "Notion",
      "Name your agents",
    ].map((label) => labels.findIndex((candidate) => candidate.startsWith(label)));
    expect(order.every((index) => index >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
  });

  it("drops unavailable connectors", async () => {
    setReads({ "/connections": { connections: [{ provider: "stripe", status: "unavailable" }, { provider: "github", status: "disconnected" }] } });
    await renderBand();
    await waitFor(() => expect(screen.getByText("GitHub")).toBeTruthy());
    expect(screen.queryByText("Stripe")).toBeNull();
  });

  it("disables admin-only cards for a member with Ask an admin", async () => {
    mockRole = "org:member";
    await renderBand();
    await waitFor(() => expect(screen.getByText("Get started")).toBeTruthy());
    const stripe = screen.getByLabelText("Stripe. Ask an admin");
    expect(stripe.props.accessibilityState).toMatchObject({ disabled: true });
    await act(async () => { fireEvent.press(stripe); });
    expect(mockGet).not.toHaveBeenCalledWith("/connections/stripe/authorize?return_to=app");
    // Naming is the one step a member can finish.
    expect(screen.getByLabelText("Name your agents. Make the starter team yours").props.accessibilityState)
      .toMatchObject({ disabled: false });
  });

  it("starter model card sells nothing and routes to the provider screen", async () => {
    await renderBand();
    const card = await screen.findByLabelText(/^You're on the starter model/);
    expect(card.props.accessibilityLabel).not.toMatch(/buy|upgrade|purchase|subscribe/i);
    expect(card.props.accessibilityLabel).toMatch(/pick/i);
    await act(async () => { fireEvent.press(card); });
    expect(mockPush).toHaveBeenCalledWith("/(app)/control/llm");
  });

  it("naming routes to the agents screen", async () => {
    await renderBand();
    const naming = await screen.findByLabelText(/^Name your agents/);
    await act(async () => { fireEvent.press(naming); });
    expect(mockPush).toHaveBeenCalledWith("/(app)/control/agents");
    expect(screen.queryByLabelText("More connections")).toBeNull();
  });

  it("refreshes after the definitive OAuth return even if the user switched back early", async () => {
    setReads({ "/connections/stripe/authorize?return_to=ios": { authorize_url: "https://connect.example/stripe" } });
    let completeAuth!: (result: { type: string; url: string }) => void;
    (WebBrowser.openAuthSessionAsync as jest.Mock).mockImplementation(
      () => new Promise((resolve) => { completeAuth = resolve; }),
    );
    await renderBand();
    const before = mockGet.mock.calls.filter(([path]) => path === "/connections").length;
    const target = await screen.findByLabelText(/^Stripe/);
    await act(async () => { fireEvent.press(target); });
    expect(WebBrowser.openAuthSessionAsync).toHaveBeenCalledWith(
      "https://connect.example/stripe", "flickmeon://checkout_redirect/connected",
    );
    expect(mockGet.mock.calls.filter(([path]) => path === "/connections").length).toBe(before);
    await act(async () => { appStateChanged?.("background"); });
    expect(mockGet.mock.calls.filter(([path]) => path === "/connections").length).toBe(before);
    await act(async () => { appStateChanged?.("active"); });
    expect(mockGet.mock.calls.filter(([path]) => path === "/connections").length).toBe(before + 1);
    await act(async () => {
      completeAuth({ type: "success", url: "flickmeon://checkout_redirect/connected?connected=stripe" });
    });
    expect(mockGet.mock.calls.filter(([path]) => path === "/connections").length).toBe(before + 2);
    await act(async () => { urlChanged?.({ url: "flickmeon://checkout_redirect/connected?connected=stripe" }); });
    await act(async () => { appStateChanged?.("active"); });
    expect(mockGet.mock.calls.filter(([path]) => path === "/connections").length).toBe(before + 2);
  });

  describe("invite", () => {
    let share: jest.SpyInstance;
    beforeEach(() => {
      share = jest.spyOn(Share, "share").mockResolvedValue({ action: Share.sharedAction });
    });
    afterEach(() => share.mockRestore());

    const invitePath = () => `/orgs/${mockOrg!.id}/join-link`;

    it("mints a link only when none exists, then shares it", async () => {
      setReads({ [invitePath()]: { exists: false } });
      await renderBand();
      const target = await screen.findByLabelText(/^Invite your team/);
      await act(async () => { fireEvent.press(target); });
      expect(mockPost).toHaveBeenCalledWith(invitePath(), {});
      expect(share).toHaveBeenCalledWith({ message: "https://thelightbulb.company/join/new" });
      expect(screen.getByText("Link shared")).toBeTruthy();
    });

    it("shares an existing link without rotating it", async () => {
      setReads({ [invitePath()]: { exists: true, join_url: "https://thelightbulb.company/join/old" } });
      await renderBand();
      const target = await screen.findByLabelText(/^Invite your team/);
      await act(async () => { fireEvent.press(target); });
      expect(mockPost).not.toHaveBeenCalled();
      expect(share).toHaveBeenCalledWith({ message: "https://thelightbulb.company/join/old" });
    });

    it("never rotates a legacy link and points at Settings → Members", async () => {
      setReads({ [invitePath()]: { exists: true, join_url: null } });
      await renderBand();
      const target = await screen.findByLabelText(/^Invite your team/);
      await act(async () => { fireEvent.press(target); });
      expect(mockPost).not.toHaveBeenCalled();
      expect(share).not.toHaveBeenCalled();
      expect(screen.getByText(/Settings → Members/)).toBeTruthy();
    });

    it("reports a failed fetch", async () => {
      setReads({ [invitePath()]: () => { throw new Error("boom"); } });
      await renderBand();
      const target = await screen.findByLabelText(/^Invite your team/);
      await act(async () => { fireEvent.press(target); });
      expect(screen.getByText("Couldn’t get the link — try again")).toBeTruthy();
    });
  });

  it("dismissal persists per workspace under the scoped key", async () => {
    await renderBand();
    const dismiss = await screen.findByLabelText("Dismiss setup");
    await act(async () => { fireEvent.press(dismiss); });
    expect(screen.queryByText("Get started")).toBeNull();
    const files: Map<string, string> = require("expo-file-system").__files;
    const stored = [...files.entries()].find(([uri]) => uri.includes("onboarding-band"));
    expect(JSON.parse(stored![1])).toMatchObject({ [dismissKey(mockOrg!.id)]: "1" });
    expect(dismissKey("org_x")).toBe("lightbulb.messaging.onboarding-band.dismissed.org_x");

    // Same workspace: stays dismissed. A different workspace: still shown.
    await renderBand();
    expect(screen.queryByText("Get started")).toBeNull();
    mockOrg = { id: "org_other", membersCount: 1 };
    await renderBand();
    await waitFor(() => expect(screen.getByText("Get started")).toBeTruthy());
  });
});
