// Node 22's built-in localStorage needs --localstorage-file; vitest runs
// without it, so storage-touching tests install this Map-backed polyfill.
// tests/setup.ts installs it once for the whole suite.
//
// The data lives in a WeakMap keyed by the storage object, and the methods live
// on `Storage.prototype` rather than on each instance. That placement is
// load-bearing: an instance with its OWN `setItem` shadows the prototype, so
// `vi.spyOn(Storage.prototype, "setItem")` would never fire and every
// assertion written against such a spy would pass for the wrong reason —
// silently, which is the worst way for a test to be wrong.

const backing = new WeakMap<object, Map<string, string>>();

function mapFor(target: object): Map<string, string> {
  let map = backing.get(target);
  if (!map) {
    map = new Map();
    backing.set(target, map);
  }
  return map;
}

/** A Storage whose methods are inherited, not own — see the file header. */
function makeStorage(): Storage {
  const hasStorage = typeof Storage !== "undefined";
  const store = (hasStorage ? Object.create(Storage.prototype) : {}) as Storage;
  backing.set(store, new Map());
  // `length` is an accessor, not a method, so nothing spies on it; keeping it
  // per-instance avoids redefining an accessor on the shared prototype.
  Object.defineProperty(store, "length", {
    get: () => mapFor(store).size,
    configurable: true,
  });
  if (!hasStorage) Object.assign(store, storageMethods());
  return store;
}

/** The five Storage methods, dispatching on `this`. */
function storageMethods(): Record<string, unknown> {
  return {
    getItem(this: object, key: string) {
      const map = mapFor(this);
      return map.has(key) ? map.get(key)! : null;
    },
    setItem(this: object, key: string, value: string) {
      mapFor(this).set(key, String(value));
    },
    removeItem(this: object, key: string) {
      mapFor(this).delete(key);
    },
    clear(this: object) {
      mapFor(this).clear();
    },
    key(this: object, index: number) {
      return Array.from(mapFor(this).keys())[index] ?? null;
    },
  };
}

function installPrototypeMethods(): () => void {
  if (typeof Storage === "undefined") return () => {};
  const proto = Storage.prototype as unknown as Record<string, unknown>;
  const methods = storageMethods();
  const saved = Object.keys(methods).map(
    (name) => [name, Object.getOwnPropertyDescriptor(proto, name)] as const,
  );
  Object.assign(proto, methods);
  return () => {
    for (const [name, desc] of saved) {
      if (desc) Object.defineProperty(proto, name, desc);
      else delete proto[name];
    }
  };
}

export function installStoragePolyfill(): () => void {
  const restoreProto = installPrototypeMethods();
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
    restoreProto();
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
