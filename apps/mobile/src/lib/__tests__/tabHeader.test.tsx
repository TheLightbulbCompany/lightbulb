import { fireEvent, render, screen } from "@testing-library/react-native";
import { useState, useSyncExternalStore, type ReactNode } from "react";
import { Text, TextInput } from "react-native";

// The (app) Stack's header, as far as this suite needs it: whatever a
// consumer hands the parent's `setOptions` is drawn — the search bar as a
// TextInput that reports text the way the native one does, and `headerRight`
// as-is. Same rig `(tabs)/__tests__/chats.test.tsx` uses for the real tabs.
type MockHeaderOptions = {
  headerSearchBarOptions?: {
    placeholder?: string;
    onChangeText?: (event: { nativeEvent: { text: string } }) => void;
  };
  headerRight?: () => ReactNode;
};
let mockHeaderOptions: MockHeaderOptions = {};
const mockHeaderListeners = new Set<() => void>();
const mockSetOptions = jest.fn((options: MockHeaderOptions) => {
  mockHeaderOptions = { ...mockHeaderOptions, ...options };
  for (const listener of mockHeaderListeners) listener();
});
// One object, as react-navigation caches it per route: the hook keys its
// effect on it, and a fresh one per render would re-run that effect.
const mockParentNavigation = { setOptions: (options: MockHeaderOptions) => mockSetOptions(options) };
function MockHeader() {
  const options = useSyncExternalStore(
    (listener) => {
      mockHeaderListeners.add(listener);
      return () => {
        mockHeaderListeners.delete(listener);
      };
    },
    () => mockHeaderOptions,
  );
  const search = options.headerSearchBarOptions;
  return (
    <>
      {search && (
        <TextInput
          placeholder={search.placeholder}
          onChangeText={(text) => search.onChangeText?.({ nativeEvent: { text } })}
        />
      )}
      {options.headerRight?.()}
    </>
  );
}

// The tab bar's selection, as the tab navigator's state reports it; the
// harness below is always the tab keyed "tab-a".
let mockSelectedTab = "tab-a";
jest.mock("expo-router", () => ({
  useNavigation: () => mockParentNavigation,
  useRoute: () => ({ key: "tab-a" }),
}));
jest.mock("expo-router/react-navigation", () => ({
  useNavigationState: <T,>(selector: (state: { index: number; routes: { key: string }[] }) => T) =>
    selector({ index: 0, routes: [{ key: mockSelectedTab }] }),
}));

import { useTabHeader } from "../tabHeader";

/**
 * A minimal consumer, shaped like every real tab (Home, Chats, …): a search
 * field it writes into the shared header while selected, cleared by
 * `onLeave` on deselect. `options` is a fresh object literal every render —
 * exactly what a real screen passes, and exactly what should NOT force the
 * header to re-apply on its own.
 */
function Harness() {
  const [query, setQuery] = useState("");
  useTabHeader(
    {
      headerSearchBarOptions: {
        placeholder: "Search",
        onChangeText: (event) => setQuery(event.nativeEvent.text),
      },
      headerRight: () => <Text>Compose</Text>,
    },
    () => setQuery(""),
  );
  return <Text testID="query">{query}</Text>;
}

const Screen = () => (
  <>
    <MockHeader />
    <Harness />
  </>
);

describe("useTabHeader", () => {
  beforeEach(() => {
    mockSetOptions.mockClear();
    mockHeaderOptions = {};
    mockSelectedTab = "tab-a";
  });

  it("empties the search and hands the header back when another tab is selected, then re-applies it on return", async () => {
    const view = await render(<Screen />);
    await fireEvent.changeText(await screen.findByPlaceholderText("Search"), "laun");
    expect(screen.getByTestId("query").props.children).toBe("laun");

    mockSelectedTab = "tab-b";
    await view.rerender(<Screen />);
    expect(mockSetOptions).toHaveBeenLastCalledWith({
      headerSearchBarOptions: undefined,
      headerRight: undefined,
    });
    expect(screen.queryByPlaceholderText("Search")).toBeNull();
    // onLeave ran on deselect, same as every real tab's own cleanup.
    expect(screen.getByTestId("query").props.children).toBe("");

    mockSelectedTab = "tab-a";
    await view.rerender(<Screen />);
    expect(screen.getByPlaceholderText("Search")).toBeTruthy();
  });

  // A screen pushed on top of the tabs blurs the TAB SCREEN's own focus, but
  // never changes which tab is selected — and selection, not focus, is what
  // this hook keys on (see the module doc). Simulate the screen re-rendering
  // for an unrelated reason (any state change hands the hook a brand-new
  // options object) while still selected, and confirm the header is left
  // exactly where it was: it rides a push out whole, search box included,
  // the same guarantee `home.test.tsx` used to cover directly.
  it("keeps the header applied across a re-render while the tab stays selected", async () => {
    await render(<Screen />);
    mockSetOptions.mockClear();
    await fireEvent.changeText(await screen.findByPlaceholderText("Search"), "laun");
    expect(mockSetOptions).not.toHaveBeenCalled();
    expect(screen.getByPlaceholderText("Search")).toBeTruthy();
    expect(screen.getByTestId("query").props.children).toBe("laun");
  });
});
