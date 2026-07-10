jest.mock("expo-secure-store", () => ({
  getItemAsync: jest.fn(async () => null),
  setItemAsync: jest.fn(async () => {}),
  deleteItemAsync: jest.fn(async () => {}),
}));

// expo-notifications and expo-device are installed by Task 8 ("expo-notifications
// deps + iOS config"), which runs after this task. Until then they are not
// resolvable, so these mocks are `virtual`. Virtual mocks keep intercepting the
// module once Task 8 installs the real package, so no change is needed later.
jest.mock(
  "expo-notifications",
  () => ({
    getPermissionsAsync: jest.fn(async () => ({ status: "granted" })),
    requestPermissionsAsync: jest.fn(async () => ({ status: "granted" })),
    getExpoPushTokenAsync: jest.fn(async () => ({ data: "ExponentPushToken[test]" })),
    setNotificationHandler: jest.fn(),
    addNotificationResponseReceivedListener: jest.fn(() => ({ remove: jest.fn() })),
  }),
  { virtual: true },
);

jest.mock("expo-device", () => ({ isDevice: true }), { virtual: true });
