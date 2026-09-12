jest.mock("expo-secure-store", () => ({
  getItemAsync: jest.fn(async () => null),
  setItemAsync: jest.fn(async () => {}),
  deleteItemAsync: jest.fn(async () => {}),
  // Keychain-accessibility constant (matches the native module's value shape).
  AFTER_FIRST_UNLOCK: "afterFirstUnlock",
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

// Attachment pickers: real deps, mocked for determinism (no native module).
// Default to a canceled pick; suites drive the jest.fn per-test.
jest.mock(
  "expo-image-picker",
  () => ({
    launchImageLibraryAsync: jest.fn(async () => ({ canceled: true, assets: null })),
  }),
  { virtual: true },
);

jest.mock(
  "expo-document-picker",
  () => ({
    getDocumentAsync: jest.fn(async () => ({ canceled: true, assets: null })),
  }),
  { virtual: true },
);

// Clipboard: a real dep, mocked so no suite reaches the native module. Suites
// that assert WHAT was copied re-mock it with their own spy.
jest.mock("expo-clipboard", () => ({ setStringAsync: jest.fn(async () => true) }), {
  virtual: true,
});
