import { LocalStore } from "../localStore";

// The jest-setup stand-in for expo-file-system keeps written text in memory, so
// a second store over the same name reads back what the first one flushed.

describe("LocalStore", () => {
  it("serves what was written, before it reaches the disk", () => {
    const store = new LocalStore<{ a?: string }>("serves");
    store.write({ a: "one" });
    expect(store.read()).toEqual({ a: "one" });
  });

  it("reads back what a previous store flushed", () => {
    const first = new LocalStore<{ a?: string }>("round-trip");
    first.write({ a: "kept" });
    first.flush();
    expect(new LocalStore<{ a?: string }>("round-trip").read()).toEqual({ a: "kept" });
  });

  // Anything here is a convenience. A store that cannot be parsed is an empty
  // store, never a crash on a screen the reader was just trying to open.
  it("is empty rather than broken when the file is corrupt", () => {
    const store = new LocalStore<{ a?: string }>("corrupt");
    store.write({ a: "fine" });
    store.flush();
    corrupt("corrupt");
    expect(new LocalStore<{ a?: string }>("corrupt").read()).toEqual({});
  });

  it("debounces the disk write", () => {
    jest.useFakeTimers();
    const store = new LocalStore<{ n?: number }>("debounced");
    store.write({ n: 1 });
    store.write({ n: 2 });
    store.write({ n: 3 });
    expect(written("debounced")).toBeUndefined();
    jest.advanceTimersByTime(1000);
    expect(JSON.parse(written("debounced") ?? "{}")).toEqual({ n: 3 });
    jest.useRealTimers();
  });
});

// Reaches into the jest-setup mock's backing map.
type FakeFs = { __files: Map<string, string> };
const files = () => (require("expo-file-system") as unknown as FakeFs).__files;
const written = (name: string) =>
  [...files().entries()].find(([uri]) => uri.endsWith(`${name}.json`))?.[1];
const corrupt = (name: string) => {
  const entry = [...files().keys()].find((uri) => uri.endsWith(`${name}.json`));
  if (entry) files().set(entry, "{not json");
};
