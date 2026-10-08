import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { ApiError } from "@lightbulb/shared";
import * as Clipboard from "expo-clipboard";

jest.mock("expo-clipboard", () => ({ setStringAsync: jest.fn(async () => true) }));
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: async () => "token" }),
  useOrganization: () => ({ organization: { id: "org_1" } }),
}));

const BASE = "/orgs/org_1";
let mockList: unknown;
const mockGet = jest.fn(async (path: string): Promise<unknown> => {
  if (path === `${BASE}/invitations`) {
    if (mockList instanceof Error) throw mockList;
    return { invitations: mockList };
  }
  if (path === `${BASE}/join-link`) return { exists: true, join_url: "https://thelightbulb.company/join/abc" };
  throw new Error(`unexpected GET ${path}`);
});
const mockPost = jest.fn(async (_path: string, body: unknown): Promise<unknown> => {
  const { email } = body as { email: string };
  if (email === "taken@x.com") throw new ApiError(409, { detail: "That email already has a pending invitation." });
  if (email === "bad") throw new ApiError(422, { detail: [] });
  return { id: `inv_${email}`, email, created_at: new Date().toISOString(), expires_at: null, status: "pending" };
});
const mockDel = jest.fn(async (_path: string): Promise<unknown> => undefined);
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({ get: mockGet, post: mockPost, del: mockDel }),
}));

import InviteScreen from "../invite";

const twoHoursAgo = new Date(Date.now() - 2 * 3600_000).toISOString();
const ada = { id: "inv_ada", email: "ada@x.com", created_at: twoHoursAgo, expires_at: null, status: "pending" };

async function renderScreen() {
  await render(<InviteScreen />);
  await act(async () => { await Promise.resolve(); });
}

beforeEach(() => {
  mockList = [ada];
  mockGet.mockClear();
  mockPost.mockClear();
  mockDel.mockReset().mockImplementation(async () => undefined);
});

describe("Invite people", () => {
  it("lists pending invitations with when they were sent", async () => {
    await renderScreen();
    expect(await screen.findByText("ada@x.com")).toBeTruthy();
    expect(screen.getByText("Sent 2h ago")).toBeTruthy();
  });

  it("sends one invite per address and shows a line for each", async () => {
    await renderScreen();
    await fireEvent.changeText(screen.getByLabelText("Email addresses"), "bo@x.com, cy@x.com");
    await fireEvent.press(screen.getByLabelText("Send invites"));
    expect(mockPost).toHaveBeenCalledTimes(2);
    expect(mockPost).toHaveBeenCalledWith(`${BASE}/invitations`, { email: "bo@x.com" });
    expect(screen.getByText("bo@x.com: Invite sent")).toBeTruthy();
    expect(screen.getByText("cy@x.com: Invite sent")).toBeTruthy();
    expect(screen.getByLabelText("Email addresses").props.value).toBe("");
    expect(screen.getByText("cy@x.com")).toBeTruthy();
  });

  it("shows the server's 409 text as given and keeps failed addresses in the field", async () => {
    await renderScreen();
    await fireEvent.changeText(screen.getByLabelText("Email addresses"), "taken@x.com bad ok@x.com");
    await fireEvent.press(screen.getByLabelText("Send invites"));
    expect(screen.getByText("taken@x.com: That email already has a pending invitation.")).toBeTruthy();
    expect(screen.getByText("bad: That doesn't look like an email address.")).toBeTruthy();
    expect(screen.getByLabelText("Email addresses").props.value).toBe("taken@x.com, bad");
  });

  it("revokes optimistically", async () => {
    await renderScreen();
    await fireEvent.press(await screen.findByLabelText("Revoke invitation for ada@x.com"));
    expect(mockDel).toHaveBeenCalledWith(`${BASE}/invitations/inv_ada`);
    expect(screen.queryByText("ada@x.com")).toBeNull();
  });

  it("restores a row whose revoke failed", async () => {
    mockDel.mockImplementation(async () => { throw new Error("offline"); });
    await renderScreen();
    await fireEvent.press(await screen.findByLabelText("Revoke invitation for ada@x.com"));
    await waitFor(() => expect(screen.getByText("ada@x.com")).toBeTruthy());
    expect(screen.getByText("Couldn't revoke ada@x.com. Try again.")).toBeTruthy();
  });

  it("shows a member only the ask-an-admin line", async () => {
    mockList = new ApiError(403, { detail: "admin only" });
    await renderScreen();
    expect(await screen.findByText("Only a workspace admin can invite people. Ask an admin.")).toBeTruthy();
    expect(screen.queryByLabelText("Email addresses")).toBeNull();
    expect(screen.queryByLabelText("Copy join link")).toBeNull();
  });

  it("takes one copy at a time so a double tap cannot mint two links", async () => {
    let release!: () => void;
    const held = new Promise<unknown>((resolve) => { release = () => resolve({ exists: true, join_url: "https://thelightbulb.company/join/abc" }); });
    mockGet.mockImplementation(async (path: string) => {
      if (path === `${BASE}/invitations`) return { invitations: mockList };
      if (path === `${BASE}/join-link`) return held;
      throw new Error(`unexpected GET ${path}`);
    });
    await renderScreen();
    const button = screen.getByLabelText("Copy join link");
    await fireEvent.press(button);
    await fireEvent.press(button);
    expect(mockGet.mock.calls.filter(([path]) => path === `${BASE}/join-link`)).toHaveLength(1);
    expect(button).toBeDisabled();
    await act(async () => { release(); await Promise.resolve(); });
    expect(screen.getByText("Join link copied.")).toBeTruthy();
    expect(button).toBeEnabled();
  });

  it("copies the existing join link without minting a new one", async () => {
    await renderScreen();
    await fireEvent.press(screen.getByLabelText("Copy join link"));
    expect(Clipboard.setStringAsync).toHaveBeenCalledWith("https://thelightbulb.company/join/abc");
    expect(mockPost).not.toHaveBeenCalled();
    expect(screen.getByText("Join link copied.")).toBeTruthy();
  });
});
