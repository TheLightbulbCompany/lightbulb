import { ApiError } from "@lightbulb/shared";
import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { ActionSheetIOS, Alert } from "react-native";

let mockRole = "org:admin";
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: async () => "test-token" }),
  useOrganization: () => ({ organization: { id: "org_1" }, membership: { role: mockRole } }),
}));

jest.mock("expo-router", () => {
  const React = jest.requireActual("react");
  return { useFocusEffect: (cb: () => void) => React.useEffect(cb, [cb]) };
});

const mockApi = { get: jest.fn(), post: jest.fn(), patch: jest.fn(), del: jest.fn() };
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => mockApi,
}));

import WebsiteSignIns from "../website-signins";

const LOGIN = { id: "vault_0123456789ab", origin: "https://accounts.google.com", username: "dana@example.com",
  addedBy: "user_1", addedByName: "dana@example.com", createdAt: "2026-10-01T00:00:00Z", lastUsedAt: null };
const SITES = { sites: [
  { site: "google.com", signedIn: true, logins: [LOGIN] },
  { site: "linkedin.com", signedIn: false, logins: [{ ...LOGIN, id: "vault_ba9876543210", origin: "https://www.linkedin.com", username: "growth" }] },
] };

/** Open a row's menu and choose one entry; returns the alert spy for the confirm that may follow. */
async function choose(rowLabel: string, entry: string) {
  const sheet = jest.spyOn(ActionSheetIOS, "showActionSheetWithOptions").mockImplementation(() => {});
  await fireEvent.press(await screen.findByLabelText(rowLabel));
  const [options, callback] = sheet.mock.calls.at(-1)!;
  await act(async () => { callback(options.options.indexOf(entry)); });
  return options.options;
}

beforeEach(() => {
  jest.clearAllMocks();
  mockRole = "org:admin";
  mockApi.get.mockResolvedValue(SITES);
  mockApi.post.mockResolvedValue(undefined);
  mockApi.patch.mockResolvedValue(LOGIN);
  mockApi.del.mockResolvedValue(undefined);
});

it("lists each site with its usernames and the browser marker", async () => {
  await render(<WebsiteSignIns />);
  expect(await screen.findByText("google.com")).toBeTruthy();
  expect(mockApi.get).toHaveBeenCalledWith("/website-logins/sites");
  expect(screen.getByText("dana@example.com")).toBeTruthy();
  expect(screen.getByText("linkedin.com")).toBeTruthy();
  expect(screen.getByText("growth")).toBeTruthy();
  expect(screen.getAllByText("Browser session saved")).toHaveLength(1);
});

it("shows the empty state when there are none", async () => {
  mockApi.get.mockResolvedValue({ sites: [] });
  await render(<WebsiteSignIns />);
  expect(await screen.findByText("No website sign-ins yet. When an agent needs one, it asks here.")).toBeTruthy();
});

const NOT_FOUND = new ApiError(404, { detail: "Not Found" }, "Not Found");
/** A backend with only GET/POST/DELETE /website-logins. */
const oldBackend = (logins: unknown[]) =>
  mockApi.get.mockImplementation(async (path: string) => {
    if (path === "/website-logins") return { logins };
    throw NOT_FOUND;
  });

it("lists from the flat login route, one card per exact host, when there is no sites route", async () => {
  oldBackend([LOGIN, { ...LOGIN, id: "vault_aaaaaaaaaaaa", origin: "https://mail.google.com:8443", username: "ops" },
    { ...LOGIN, id: "vault_ba9876543210", origin: "https://www.linkedin.com", username: "growth" }]);
  await render(<WebsiteSignIns />);
  expect(await screen.findByText("accounts.google.com")).toBeTruthy();
  expect(mockApi.get.mock.calls.map(([path]) => path)).toEqual(["/website-logins/sites", "/website-logins"]);
  expect(screen.getByText("dana@example.com")).toBeTruthy();
  // No guessed registrable domain: a different host is a different card.
  expect(screen.getByText("mail.google.com")).toBeTruthy();
  expect(screen.getByText("ops")).toBeTruthy();
  expect(screen.getByText("www.linkedin.com")).toBeTruthy();
  expect(screen.queryByText("Browser session saved")).toBeNull();
  expect(screen.queryByText(/Couldn’t load/)).toBeNull();
});

it("offers only Remove on a row, and Add, when there is no sites route", async () => {
  oldBackend([LOGIN]);
  const alert = jest.spyOn(Alert, "alert");
  await render(<WebsiteSignIns />);
  const options = await choose("Actions for dana@example.com on accounts.google.com", "Remove");
  expect(options).toEqual(["Remove", "Cancel"]);
  await act(async () => { alert.mock.calls.at(-1)![2]!.find((b) => b.text === "Remove")!.onPress!(); });
  expect(mockApi.del).toHaveBeenCalledWith("/website-logins/vault_0123456789ab");
  expect(screen.getByLabelText("Add a website sign-in")).toBeTruthy();
});

it("shows a sign-in added against a backend with no sites route", async () => {
  const logins: unknown[] = [];
  oldBackend(logins);
  mockApi.post.mockImplementation(async () => { logins.push({ ...LOGIN, origin: "https://app.example.com", username: "dana" }); });
  await render(<WebsiteSignIns />);
  expect(await screen.findByText("No website sign-ins yet. When an agent needs one, it asks here.")).toBeTruthy();
  await fireEvent.press(screen.getByLabelText("Add a website sign-in"));
  await fireEvent.changeText(screen.getByLabelText("Website address"), "app.example.com");
  await fireEvent.changeText(screen.getByLabelText("Username or email"), "dana");
  await fireEvent.changeText(screen.getByLabelText("Password"), "hunter2");
  await fireEvent.press(screen.getByRole("button", { name: "Add" }));
  expect(await screen.findByText("app.example.com")).toBeTruthy();
  expect(screen.getByText("dana")).toBeTruthy();
});

it("shows the empty state, not an error, when neither list route exists", async () => {
  mockApi.get.mockRejectedValue(NOT_FOUND);
  await render(<WebsiteSignIns />);
  expect(await screen.findByText("No website sign-ins yet. When an agent needs one, it asks here.")).toBeTruthy();
  expect(screen.queryByText(/Couldn’t load/)).toBeNull();
});

it("tells the person why a browser sign-out was refused", async () => {
  mockApi.post.mockRejectedValue(new ApiError(409, { detail: "An agent is using the browser. Try again in a moment." },
    "An agent is using the browser. Try again in a moment."));
  const alert = jest.spyOn(Alert, "alert");
  await render(<WebsiteSignIns />);
  await choose("Actions for dana@example.com on google.com", "Sign out of the browser");
  await act(async () => { alert.mock.calls.at(-1)![2]!.find((b) => b.text === "Sign out")!.onPress!(); });
  expect(alert).toHaveBeenLastCalledWith("Couldn’t sign out", "An agent is using the browser. Try again in a moment.");
});

it("offers only the browser sign-out for a site signed in with cookies alone", async () => {
  mockApi.get.mockResolvedValue({ sites: [{ site: "github.com", signedIn: true, logins: [] }] });
  await render(<WebsiteSignIns />);
  expect(await screen.findByText("No saved password")).toBeTruthy();
  expect(await choose("Actions for github.com", "Cancel")).toEqual(["Sign out of the browser", "Cancel"]);
});

it("shows an error with a retry", async () => {
  mockApi.get.mockRejectedValueOnce(new ApiError(500, null, "Server on fire"));
  await render(<WebsiteSignIns />);
  expect(await screen.findByText("Couldn’t load website sign-ins: Server on fire")).toBeTruthy();
  await fireEvent.press(screen.getByLabelText("Retry"));
  expect(await screen.findByText("google.com")).toBeTruthy();
});

it("never asks the server for a member, and says who manages sign-ins", async () => {
  mockRole = "org:member";
  await render(<WebsiteSignIns />);
  expect(await screen.findByText("Website sign-ins are managed by your workspace admin.")).toBeTruthy();
  expect(mockApi.get).not.toHaveBeenCalled();
  expect(screen.queryByLabelText("Add a website sign-in")).toBeNull();
});

it("treats a 403 as the same restricted state", async () => {
  mockApi.get.mockRejectedValue(new ApiError(403, null, "Forbidden"));
  await render(<WebsiteSignIns />);
  expect(await screen.findByText("Website sign-ins are managed by your workspace admin.")).toBeTruthy();
});

it("adds a sign-in", async () => {
  await render(<WebsiteSignIns />);
  await fireEvent.press(await screen.findByLabelText("Add a website sign-in"));
  await fireEvent.changeText(screen.getByLabelText("Website address"), "app.example.com");
  await fireEvent.changeText(screen.getByLabelText("Username or email"), "dana");
  await fireEvent.changeText(screen.getByLabelText("Password"), "hunter2");
  await fireEvent.press(screen.getByRole("button", { name: "Add" }));
  expect(mockApi.post).toHaveBeenCalledWith("/website-logins", { origin: "app.example.com", username: "dana", password: "hunter2" });
});

it("edits a username and replaces a password with separate PATCHes", async () => {
  await render(<WebsiteSignIns />);
  const options = await choose("Actions for dana@example.com on google.com", "Edit username");
  expect(options).toEqual(["Edit username", "Replace password", "Sign out of the browser", "Remove", "Cancel"]);
  expect(screen.getByLabelText("Username or email").props.value).toBe("dana@example.com");
  expect(screen.queryByLabelText("Password")).toBeNull();
  await fireEvent.changeText(screen.getByLabelText("Username or email"), "dana2@example.com");
  await fireEvent.press(screen.getByRole("button", { name: "Save" }));
  expect(mockApi.patch).toHaveBeenCalledWith("/website-logins/vault_0123456789ab", { username: "dana2@example.com" });

  await choose("Actions for dana@example.com on google.com", "Replace password");
  await fireEvent.changeText(await screen.findByLabelText("Password"), "new-secret");
  await fireEvent.press(screen.getByRole("button", { name: "Save" }));
  expect(mockApi.patch).toHaveBeenLastCalledWith("/website-logins/vault_0123456789ab", { password: "new-secret" });
});

it("removes and signs out only after a confirm", async () => {
  const alert = jest.spyOn(Alert, "alert");
  await render(<WebsiteSignIns />);
  await choose("Actions for dana@example.com on google.com", "Remove");
  expect(mockApi.del).not.toHaveBeenCalled();
  await act(async () => { alert.mock.calls.at(-1)![2]!.find((b) => b.text === "Remove")!.onPress!(); });
  expect(mockApi.del).toHaveBeenCalledWith("/website-logins/vault_0123456789ab");

  await choose("Actions for dana@example.com on google.com", "Sign out of the browser");
  expect(alert.mock.calls.at(-1)![1]).toBe("Agents will be signed out of google.com in the workspace browser.");
  expect(mockApi.post).not.toHaveBeenCalled();
  await act(async () => { alert.mock.calls.at(-1)![2]!.find((b) => b.text === "Sign out")!.onPress!(); });
  expect(mockApi.post).toHaveBeenCalledWith("/website-logins/sites/sign-out", { site: "google.com" });
});

it("offers no browser sign-out for a site that is not signed in", async () => {
  await render(<WebsiteSignIns />);
  const options = await choose("Actions for growth on linkedin.com", "Cancel");
  expect(options).not.toContain("Sign out of the browser");
});
