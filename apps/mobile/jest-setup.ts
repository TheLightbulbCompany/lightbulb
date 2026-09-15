// react-native-gesture-handler's own jest setup: swaps its native module for a
// JS-only stand-in so `Gesture.Pan()`/`GestureDetector` (BottomSheet's
// drag-to-dismiss) can be driven in tests via `fireGestureHandler`.
import "react-native-gesture-handler/jestSetup";

// react-native-reanimated: activates the manual mock at
// __mocks__/react-native-reanimated.js (see that file's header comment for
// why — neither the real package nor its own shipped `/mock` load cleanly
// under jest on this installed version). Covers `useSharedValue` /
// `useAnimatedStyle` / `withSpring` / `Animated.createAnimatedComponent`, so
// `lib/pressScale.ts`'s icon-button scale-on-press runs on it too — extend
// that mock file, not this line, if another branch's reanimated usage needs
// more surface.
jest.mock("react-native-reanimated");

// react-native's own FileReader (used by blobToDataUri, which the attachment
// viewer's file-caching also goes through) isn't polyfilled by this jest
// environment — jest-expo's preset runs on Node, not jsdom, so `Blob` is
// global here but `FileReader` isn't. A minimal readAsDataURL is enough for
// every current caller (they all just want the base64 payload back out).
function arrayBufferToBase64(buffer: ArrayBuffer): string {
  let binary = "";
  for (const byte of new Uint8Array(buffer)) binary += String.fromCharCode(byte);
  return btoa(binary);
}
class FakeFileReader {
  result: string | null = null;
  onloadend: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readAsDataURL(blob: Blob) {
    void blob
      .arrayBuffer()
      .then((buffer) => {
        const base64 = arrayBufferToBase64(buffer);
        this.result = `data:${blob.type || "application/octet-stream"};base64,${base64}`;
        this.onloadend?.();
      })
      .catch(() => this.onerror?.());
  }
}
(globalThis as { FileReader?: unknown }).FileReader = FakeFileReader;

// expo-file-system: a real dep (the attachment viewer caches a downloaded
// attachment on disk before rendering/sharing it). Mocked with a minimal
// in-memory-ish stand-in for File/Directory/Paths so tests never touch the
// native filesystem.
jest.mock(
  "expo-file-system",
  () => {
    const joinUri = (parts: unknown[]) =>
      `file:///fake/${parts
        .map((part) => (typeof part === "string" ? part : ((part as { uri?: string })?.uri ?? "")))
        .join("/")}`;
    class FakeDirectory {
      uri: string;
      constructor(...parts: unknown[]) {
        this.uri = joinUri(parts);
      }
    }
    class FakeFile {
      uri: string;
      exists = false;
      constructor(...parts: unknown[]) {
        this.uri = joinUri(parts);
      }
      create() {
        this.exists = true;
      }
      write() {}
      delete() {
        this.exists = false;
      }
    }
    return { File: FakeFile, Directory: FakeDirectory, Paths: { cache: new FakeDirectory("cache") } };
  },
  { virtual: true },
);

// expo-sharing: a real dep, mocked so the "share/download" chrome action and
// the fallback card's "Open" button never reach the native share sheet.
jest.mock(
  "expo-sharing",
  () => ({
    isAvailableAsync: jest.fn(async () => true),
    shareAsync: jest.fn(async () => {}),
  }),
  { virtual: true },
);

// react-native-webview: a real dep, used only to render a cached PDF. Mocked
// as a bare View carrying the source uri as its accessibility label, so tests
// can assert a PDF page rendered without a native WKWebView.
jest.mock(
  "react-native-webview",
  () => {
    // Required inside the factory: jest hoists jest.mock calls above imports,
    // so the factory can't close over the module-level `React` import.
    const react = require("react");
    const { View } = require("react-native");
    return {
      WebView: (props: { source?: { uri?: string } }) =>
        react.createElement(View, {
          testID: "mock-webview",
          accessibilityLabel: props.source?.uri,
        }),
    };
  },
  { virtual: true },
);

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
    useLastNotificationResponse: jest.fn(() => null),
    clearLastNotificationResponse: jest.fn(),
    setNotificationHandler: jest.fn(),
    setBadgeCountAsync: jest.fn(async () => true),
  }),
  { virtual: true },
);

jest.mock("expo-device", () => ({ isDevice: true }), { virtual: true });

// Attachment pickers: real deps, mocked for determinism (no native module).
// Default to a canceled pick; suites drive the jest.fn per-test.
jest.mock(
  "expo-image-picker",
  () => ({
    requestMediaLibraryPermissionsAsync: jest.fn(async () => ({ granted: true, status: "granted" })),
    requestCameraPermissionsAsync: jest.fn(async () => ({ granted: true, status: "granted" })),
    launchImageLibraryAsync: jest.fn(async () => ({ canceled: true, assets: null })),
    launchCameraAsync: jest.fn(async () => ({ canceled: true, assets: null })),
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

// Haptics: a real dep, mocked so no suite reaches the native Taptic engine.
// Suites asserting a specific call re-mock it with their own spy (matching
// `lib/haptics.ts`'s two-call surface).
jest.mock(
  "expo-haptics",
  () => ({
    impactAsync: jest.fn(async () => {}),
    selectionAsync: jest.fn(async () => {}),
    ImpactFeedbackStyle: { Light: "light", Medium: "medium", Heavy: "heavy" },
  }),
  { virtual: true },
);

// react-native-keyboard-controller: a real NATIVE dep (the composer on both
// room screens rides its keyboard observer). Global, because the provider is
// mounted at the app root and the KAV wraps both room screens.
//
// Deliberately an INLINE stub rather than the library's shipped
// `react-native-keyboard-controller/jest` mock: that file is ESM and drags a
// chain of untransformed imports into every suite that touches it. Only the
// two components the app actually renders need standing in for.
jest.mock("react-native-keyboard-controller", () => {
  // Required inside the factory: jest hoists jest.mock above the imports.
  const { View } = require("react-native");
  return {
    KeyboardProvider: ({ children }: { children?: unknown }) => children,
    KeyboardAvoidingView: View,
  };
});

// @expo/ui: native SwiftUI views (BottomSheet presents through them). Mocked so
// a sheet's React Native content renders inline while it is presented, and a
// suite can simulate the reader dismissing it via `onIsPresentedChange`.
jest.mock("@expo/ui", () => ({
  Host: ({ children }: { children?: unknown }) => children,
}));
jest.mock("@expo/ui/swift-ui", () => {
  const react = require("react");
  const { View } = require("react-native");
  const passthrough = ({ children }: { children?: unknown }) => children;
  return {
    BottomSheet: (props: { isPresented: boolean; children?: unknown }) =>
      props.isPresented ? react.createElement(View, { testID: "native-sheet", ...props }) : null,
    Group: passthrough,
    RNHostView: passthrough,
  };
});
jest.mock("@expo/ui/swift-ui/modifiers", () => ({
  environment: () => ({}),
  interactiveDismissDisabled: () => ({}),
  presentationBackground: () => ({}),
  presentationDetents: () => ({}),
  presentationDragIndicator: () => ({}),
}));
