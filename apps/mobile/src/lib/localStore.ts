import { Directory, File, Paths } from "expo-file-system";

// ---------------------------------------------------------------------------
// A small JSON store on disk, for the per-device conveniences the web app keeps
// in `localStorage` — composer drafts, recently used emoji.
//
// It is `expo-file-system` and not AsyncStorage on purpose: the app ships these
// screens over the air, and AsyncStorage is a native module that is not in the
// current binary. Adding it would change the native fingerprint, which means
// no OTA can reach an installed build until a new one clears TestFlight.
// `expo-file-system` is already linked (the attachment viewer caches through
// it), so this costs nothing at the native layer.
//
// `Paths.document` and not `Paths.cache`: iOS evicts the cache directory under
// storage pressure, and a half-written message the reader left in a room is
// not something to lose to that.
//
// Every operation is best-effort. A store that cannot be read is an empty
// store, and a write that fails is dropped — this holds conveniences, never
// anything the app's correctness rests on.
// ---------------------------------------------------------------------------

type Json = Record<string, unknown>;

/** Debounce for writes. Typing should not hit the disk on every keystroke. */
const WRITE_DELAY_MS = 400;

export class LocalStore<T extends Json> {
  private readonly name: string;
  private cache: T | null = null;
  private pending: ReturnType<typeof setTimeout> | null = null;

  constructor(name: string) {
    this.name = name;
  }

  private file(): File {
    return new File(Paths.document, "store", `${this.name}.json`);
  }

  /** The whole store. Read once from disk, then served from memory. */
  read(): T {
    if (this.cache) return this.cache;
    let loaded: T;
    try {
      const file = this.file();
      const raw = file.exists ? file.textSync() : null;
      const parsed: unknown = raw ? JSON.parse(raw) : null;
      loaded = parsed && typeof parsed === "object" ? (parsed as T) : ({} as T);
    } catch {
      loaded = {} as T;
    }
    this.cache = loaded;
    return loaded;
  }

  /** Replaces the store. The disk write is debounced; the read is immediate. */
  write(next: T): void {
    this.cache = next;
    if (this.pending) clearTimeout(this.pending);
    this.pending = setTimeout(() => {
      this.pending = null;
      this.flush();
    }, WRITE_DELAY_MS);
  }

  /** Writes now rather than on the debounce — for backgrounding. */
  flush(): void {
    if (this.pending) {
      clearTimeout(this.pending);
      this.pending = null;
    }
    try {
      const directory = new Directory(Paths.document, "store");
      if (!directory.exists) directory.create({ intermediates: true });
      const file = this.file();
      if (!file.exists) file.create();
      file.write(JSON.stringify(this.cache ?? {}));
    } catch {
      /* per-device convenience only */
    }
  }
}
