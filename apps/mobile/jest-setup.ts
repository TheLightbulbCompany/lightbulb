jest.mock("expo-secure-store", () => ({
  getItemAsync: jest.fn(async () => null),
  setItemAsync: jest.fn(async () => {}),
  deleteItemAsync: jest.fn(async () => {}),
}));

// expo-notifications / expo-device are real deps, but we mock them here so unit
// tests are deterministic (no native module, fixed permission/token). `virtual`
// keeps the mock intercepting regardless of native resolution.
jest.mock(
  "expo-notifications",
  () => ({
    getPermissionsAsync: jest.fn(async () => ({ status: "granted" })),
    requestPermissionsAsync: jest.fn(async () => ({ status: "granted" })),
    getExpoPushTokenAsync: jest.fn(async () => ({ data: "ExponentPushToken[test]" })),
    getLastNotificationResponseAsync: jest.fn(async () => null),
    setNotificationHandler: jest.fn(),
    addNotificationResponseReceivedListener: jest.fn(() => ({ remove: jest.fn() })),
  }),
  { virtual: true },
);

jest.mock("expo-device", () => ({ isDevice: true }), { virtual: true });
