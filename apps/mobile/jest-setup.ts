// react-native-gesture-handler's own jest setup: swaps its native module for a
// JS-only stand-in so `Gesture.Pan()`/`GestureDetector` (BottomSheet's
// drag-to-dismiss) can be driven in tests via `fireGestureHandler`.
import "react-native-gesture-handler/jestSetup";

// react-native exports its components as LAZY getters: the first render that
// touches ScrollView/FlatList/… requires (and, on a cold transform cache as on
// CI, babel-transforms) that whole module tree inside the test body, which blew
// the 5 s per-test budget on the Linux runner (PaywallScreen's first render took
// 3.2 s with --no-cache locally, 83 ms once pre-loaded). Load them here, in setup,
// where no per-test timeout applies.
import { ActivityIndicator, FlatList, Pressable, ScrollView, TextInput } from "react-native";
void [ActivityIndicator, FlatList, Pressable, ScrollView, TextInput];

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
// attachment on disk before rendering/sharing it, and `lib/localStore` keeps
// drafts and emoji recents there). Mocked with an in-memory stand-in for
// File/Directory/Paths so tests never touch the native filesystem — contents
// live in the exported `__files` map, keyed by uri, so a suite can read back
// what was written or corrupt it on purpose.
jest.mock(
  "expo-file-system",
  () => {
    const files = new Map<string, string>();
    // A `file://` uri is already a whole path (a picked photo's), as it is
    // for the real File; anything else is joined under the fake root.
    const joinUri = (parts: unknown[]) => {
      const path = parts
        .map((part) => (typeof part === "string" ? part : ((part as { uri?: string })?.uri ?? "")))
        .join("/");
      return path.startsWith("file://") ? path : `file:///fake/${path}`;
    };
    class FakeDirectory {
      uri: string;
      constructor(...parts: unknown[]) {
        this.uri = joinUri(parts);
      }
      get exists() {
        return true;
      }
      create() {}
      delete() {
        for (const uri of [...files.keys()]) if (uri.startsWith(`${this.uri}/`)) files.delete(uri);
      }
    }
    class FakeFile {
      uri: string;
      constructor(...parts: unknown[]) {
        this.uri = joinUri(parts);
      }
      get exists() {
        return files.has(this.uri);
      }
      create() {
        if (!files.has(this.uri)) files.set(this.uri, "");
      }
      write(contents: string) {
        files.set(this.uri, contents);
      }
      textSync() {
        return files.get(this.uri) ?? "";
      }
      delete() {
        files.delete(this.uri);
      }
      get size() {
        return files.has(this.uri) ? (files.get(this.uri) ?? "").length : null;
      }
      get name() {
        return this.uri.split("/").pop() ?? "";
      }
      async bytes() {
        return new TextEncoder().encode(files.get(this.uri) ?? "");
      }
      // Attachments download straight to a file (`lib/attachmentFile`); what
      // lands in it here is the URL it came from.
      static async downloadFileAsync(url: string, destination: FakeFile) {
        files.set(destination.uri, url);
        return destination;
      }
    }
    return {
      File: FakeFile,
      Directory: FakeDirectory,
      Paths: { cache: new FakeDirectory("cache"), document: new FakeDirectory("document") },
      __files: files,
    };
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
    setNotificationChannelAsync: jest.fn(async () => null),
    AndroidImportance: { HIGH: 4 },
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

// Image re-encoding: a real dep, mocked so no suite reaches the native module.
// The "JPEG" it saves is a file named after its source; `resize` is a spy a
// suite can read back through `ImageManipulator.manipulate.mock.results`.
jest.mock(
  "expo-image-manipulator",
  () => ({
    SaveFormat: { JPEG: "jpeg", PNG: "png", WEBP: "webp" },
    ImageManipulator: {
      manipulate: jest.fn((uri: string) => {
        const context: { resize: jest.Mock; renderAsync: jest.Mock } = {
          resize: jest.fn((): unknown => context),
          renderAsync: jest.fn(async () => ({
            width: 1,
            height: 1,
            saveAsync: jest.fn(async () => ({ uri: `${uri}.resized.jpg`, width: 1, height: 1 })),
          })),
        };
        return context;
      }),
    },
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
jest.mock(
  "expo-clipboard",
  () => ({
    setStringAsync: jest.fn(async () => true),
    hasImageAsync: jest.fn(async () => false),
    getImageAsync: jest.fn(async () => null),
  }),
  { virtual: true },
);

// expo-paste-input wraps the real TextInput in a native paste receiver. Keep
// the wrapper visible to RNTL so composer tests can drive the same direct-paste
// entry point a person invokes from iOS edit menu.
jest.mock(
  "expo-paste-input",
  () => {
    const react = require("react");
    const { View } = require("react-native");
    return {
      TextInputWrapper: ({ children, onPaste }: { children: unknown; onPaste: (payload: unknown) => void }) =>
        react.createElement(View, { testID: "paste-input-wrapper", onPaste }, children),
    };
  },
  { virtual: true },
);

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

// @expo/ui: native SwiftUI views (the sheets, the grouped lists and the
// segmented controls). Mocked as React Native stand-ins so a suite sees their
// text and can press their controls:
// - a sheet renders its content inline while it is presented, and a suite can
//   simulate the reader dismissing it via `onIsPresentedChange`;
// - a Button is a Pressable, labelled by its `accessibilityLabel` modifier
//   (else its `label`), and a Picker is one Pressable per `tag`ged option that
//   fires only for a different, enabled option, as iOS does;
// - containers render their children, Sections their title, header and footer.
// Modifiers are plain objects: the few a stand-in reads carry their value.
jest.mock("@expo/ui", () => ({
  Host: ({ children }: { children?: unknown }) => children,
}));
jest.mock("@expo/ui/swift-ui", () => {
  const react = require("react");
  const { Pressable, Text, View } = require("react-native");
  type MockModified = { modifiers?: Record<string, unknown>[] };
  const passthrough = ({ children }: { children?: unknown }) => children;
  const text = (value: unknown) =>
    typeof value === "string" ? react.createElement(Text, null, value) : value;
  const modifier = (props: MockModified, key: string) =>
    props.modifiers?.find((config) => key in config)?.[key];
  return {
    BottomSheet: (props: { isPresented: boolean; children?: unknown }) =>
      props.isPresented ? react.createElement(View, { testID: "native-sheet", ...props }) : null,
    Button: (props: MockModified & { label?: string; onPress?: () => void; children?: unknown }) =>
      react.createElement(
        Pressable,
        {
          onPress: props.onPress,
          accessibilityRole: "button",
          accessibilityLabel: modifier(props, "accessibilityLabel") ?? props.label,
        },
        props.label ? text(props.label) : props.children,
      ),
    Form: passthrough,
    Group: passthrough,
    HStack: passthrough,
    Image: () => null,
    LabeledContent: (props: { label?: unknown; children?: unknown }) =>
      react.createElement(View, null, text(props.label), props.children),
    Picker: (props: {
      selection?: unknown;
      onSelectionChange?: (value: unknown) => void;
      children?: unknown;
    }) =>
      react.createElement(
        View,
        null,
        react.Children.map(props.children, (option: { props: MockModified }) => {
          const value = modifier(option.props, "tag");
          const disabled = modifier(option.props, "disabled") === true;
          return react.createElement(
            Pressable,
            {
              accessibilityRole: "button",
              accessibilityState: { selected: value === props.selection, disabled },
              onPress: () => {
                if (!disabled && value !== props.selection) props.onSelectionChange?.(value);
              },
            },
            option,
          );
        }),
      ),
    RNHostView: passthrough,
    Section: (props: { title?: string; header?: unknown; footer?: unknown; children?: unknown }) =>
      react.createElement(View, null, text(props.title), props.header, props.children, props.footer),
    Spacer: () => null,
    Text: (props: { children?: unknown }) => react.createElement(Text, null, props.children),
    Toggle: (props: { label?: string; isOn?: boolean; onIsOnChange?: (on: boolean) => void }) =>
      react.createElement(
        Pressable,
        {
          accessibilityRole: "switch",
          accessibilityLabel: props.label,
          accessibilityState: { checked: !!props.isOn },
          onPress: () => props.onIsOnChange?.(!props.isOn),
        },
        text(props.label),
      ),
    VStack: passthrough,
  };
});
jest.mock("@expo/ui/swift-ui/modifiers", () => ({
  accessibilityAddTraits: () => ({}),
  accessibilityLabel: (accessibilityLabel: string) => ({ accessibilityLabel }),
  accessibilityRemoveTraits: () => ({}),
  background: () => ({}),
  disabled: (disabled = true) => ({ disabled }),
  environment: () => ({}),
  font: () => ({}),
  foregroundStyle: () => ({}),
  frame: () => ({}),
  interactiveDismissDisabled: () => ({}),
  pickerStyle: () => ({}),
  presentationBackground: () => ({}),
  presentationDetents: () => ({}),
  presentationDragIndicator: () => ({}),
  redacted: () => ({}),
  scrollContentBackground: () => ({}),
  shapes: { circle: () => ({}), roundedRectangle: () => ({}) },
  tag: (tag: string | number) => ({ tag }),
  tint: () => ({}),
}));

// LiveKit (Live rooms) and its WebRTC fork are native. The (app) layout mounts the Live sheet, so
// every suite that renders it needs these to load; a suite that drives Live mocks them itself.
jest.mock("@livekit/react-native", () => ({ registerGlobals: jest.fn(), setupIOSAudioManagement: jest.fn(), useTrackVolume: () => 0 }));
jest.mock("@livekit/components-react", () => ({
  RoomContext: require("react").createContext(undefined),
  useIsSpeaking: () => false,
  useParticipantAttributes: () => ({ attributes: {} }),
  useParticipants: () => [],
  useTracks: () => [],
}));
jest.mock("@livekit/react-native-webrtc", () => ({}));
jest.mock("livekit-client", () => ({ Track: { Source: { Microphone: "microphone" } }, Room: jest.fn(), RoomEvent: {} }));
