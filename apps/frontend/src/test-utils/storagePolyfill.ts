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
  const local = makeStorage();
  const session = makeStorage();
  // Under jsdom `window` IS `globalThis`, but a plain Node environment has no
  // `window` at all, and some DOM emulations keep the two distinct. Install on
  // each DISTINCT target and capture per-target original descriptors, so
  // restore() puts back exactly what each target had (the old code installed
  // on both but only ever restored globalThis).
  const targets: object[] =
    typeof window === "undefined"
      ? [globalThis]
      : globalThis === (window as unknown)
        ? [globalThis]
        : [globalThis, window];
  const originals = targets.map((target) => ({
    target,
    localDesc: Object.getOwnPropertyDescriptor(target, "localStorage"),
    sessionDesc: Object.getOwnPropertyDescriptor(target, "sessionStorage"),
  }));
  for (const target of targets) {
    Object.defineProperty(target, "localStorage", { value: local, configurable: true });
    Object.defineProperty(target, "sessionStorage", { value: session, configurable: true });
  }
  return () => {
    for (const { target, localDesc, sessionDesc } of originals) {
      for (const [name, desc] of [
        ["localStorage", localDesc],
        ["sessionStorage", sessionDesc],
      ] as const) {
        if (desc) Object.defineProperty(target, name, desc);
        else delete (target as Record<string, unknown>)[name];
      }
    }
  };
}
