// Node 22's built-in localStorage needs --localstorage-file; vitest runs
// without it, so storage-touching tests install this Map-backed polyfill.
// Mirrors the inline pattern in inboxStorage.test.ts / desktopPermissions.test.ts.
function makeStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (key: string) => (map.has(key) ? map.get(key)! : null),
    key: (index: number) => Array.from(map.keys())[index] ?? null,
    removeItem: (key: string) => {
      map.delete(key);
    },
    setItem: (key: string, value: string) => {
      map.set(key, String(value));
    },
  } as Storage;
}

export function installStoragePolyfill(): () => void {
  const localDesc = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  const sessionDesc = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage");
  const local = makeStorage();
  const session = makeStorage();
  for (const target of [globalThis, window] as object[]) {
    Object.defineProperty(target, "localStorage", { value: local, configurable: true });
    Object.defineProperty(target, "sessionStorage", { value: session, configurable: true });
  }
  return () => {
    for (const [name, desc] of [
      ["localStorage", localDesc],
      ["sessionStorage", sessionDesc],
    ] as const) {
      if (desc) Object.defineProperty(globalThis, name, desc);
      else delete (globalThis as Record<string, unknown>)[name];
    }
  };
}
