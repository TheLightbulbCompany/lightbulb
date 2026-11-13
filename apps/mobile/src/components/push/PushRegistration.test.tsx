import { render, waitFor } from "@testing-library/react-native";

// `mock`-prefixed so jest's babel hoisting allows referencing it inside the
// jest.mock() factory below (out-of-scope vars are otherwise rejected).
const mockPost = jest.fn(
  async (_path: string, _body: Record<string, unknown>) => ({}),
);
jest.mock("@lightbulb/shared", () => ({
  createApiClient: () => ({ post: mockPost }),
}));
jest.mock("../../lib/adapter", () => ({ makeAdapter: () => ({}) }));
jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ getToken: async () => "tok", userId: "user_1" }),
}));
jest.mock("expo-constants", () => ({
  expoConfig: { extra: { eas: { projectId: "proj_1" } } },
}));
// jest-expo's native expo-crypto mock stubs randomUUID() to return undefined;
// override it so getDeviceId() yields a real string device id.
jest.mock("expo-crypto", () => ({
  randomUUID: () => "11111111-1111-4111-8111-111111111111",
}));

import { PushRegistration } from "./PushRegistration";

it("requests a token and POSTs it once", async () => {
  // RNTL 14's render() is async — await it before asserting effects.
  await render(
    <PushRegistration />,
  );
  await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
  const [path, body] = mockPost.mock.calls[0];
  expect(path).toBe("/devices");
  expect(body.token).toBe("ExponentPushToken[test]");
  expect(body.platform).toBe("ios");
  expect(typeof body.deviceId).toBe("string");
});

it("on Android, makes the default channel first and registers as android", async () => {
  const { Platform } = require("react-native");
  const Notifications = require("expo-notifications");
  const os = Platform.OS;
  Platform.OS = "android";
  mockPost.mockClear();
  try {
    await render(
      <PushRegistration />,
    );
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(Notifications.setNotificationChannelAsync).toHaveBeenCalledWith(
      "default",
      expect.objectContaining({ importance: Notifications.AndroidImportance.HIGH }),
    );
    expect(mockPost.mock.calls[0][1].platform).toBe("android");
  } finally {
    Platform.OS = os;
  }
});
