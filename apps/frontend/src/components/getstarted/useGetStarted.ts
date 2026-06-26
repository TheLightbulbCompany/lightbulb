import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useUser } from "@clerk/nextjs";
import { capture, captureException } from "@/lib/analytics";
import { STEPS, type StepId } from "./getStartedConfig";
import type { ArticleId } from "./getStartedContent";
import { onStep } from "./getStartedEvents";

interface GetStartedMeta { seen?: boolean; dismissed?: boolean; read?: ArticleId[]; done?: StepId[]; completedFired?: boolean }

export interface UseGetStarted {
  isOpen: boolean;
  minimized: boolean;
  tab: "do" | "learn";
  activeArticle: ArticleId | null;
  open: () => void;
  minimize: () => void;
  close: () => void;
  dismiss: () => void;
  setTab: (tab: "do" | "learn") => void;
  openArticle: (id: ArticleId) => void;
  backToLearn: () => void;
  completed: Record<StepId, boolean>;
  progress: { done: number; total: number };
  dismissed: boolean;
  seen: boolean;
}

// The five persisted fields, written as ONE whole object every time (see the
// persistence effect below). Bundling them is the load-bearing fix for the
// clobber bug: a partial read-modify-write against Clerk's async, non-
// optimistic `unsafeMetadata` let two same-tick writes each read the same
// stale base and drop the other's key. Local React state is the single source
// of truth; we never merge from `user.unsafeMetadata.getStarted`.
function sameList<T>(a: T[] = [], b: T[] = []): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}
function sameSnapshot(a: GetStartedMeta, b: GetStartedMeta): boolean {
  return (
    !!a.seen === !!b.seen &&
    !!a.dismissed === !!b.dismissed &&
    !!a.completedFired === !!b.completedFired &&
    sameList(a.read, b.read) &&
    sameList(a.done, b.done)
  );
}

export function useGetStarted(opts: { derived: Partial<Record<StepId, boolean>> }): UseGetStarted {
  const { isLoaded, user } = useUser();
  const stored = (user?.unsafeMetadata?.getStarted as GetStartedMeta | undefined) ?? {};

  const [done, setDone] = useState<StepId[]>(stored.done ?? []);
  const [read, setRead] = useState<ArticleId[]>(stored.read ?? []);
  const [dismissed, setDismissed] = useState<boolean>(!!stored.dismissed);
  const [seen, setSeen] = useState<boolean>(!!stored.seen);
  // `completedFired` is persisted state (it drives the write effect) AND
  // guarded by a ref for the one-time analytics emit (the ref is the
  // StrictMode-safe gate; see the completion effect).
  const [completedFired, setCompletedFired] = useState<boolean>(!!stored.completedFired);
  const [isOpen, setIsOpen] = useState(false);
  const [minimized, setMinimized] = useState(false);
  const [tab, setTab] = useState<"do" | "learn">("do");
  const [activeArticle, setActiveArticle] = useState<ArticleId | null>(null);

  // Hydrate local state from the loaded Clerk metadata exactly once. On a hard
  // page load `useUser()` first returns `{ isLoaded: false, user: null }`, so a
  // returning user's persisted state is not available at mount — without this
  // we would auto-open the guide and reset completion on every reload. The
  // first-run auto-open is folded in here so it respects the loaded `seen`.
  const hydratedRef = useRef(false);
  // Gates the persistence effect so it stays quiet until local state has been
  // hydrated. It is STATE (not the ref above) on purpose: the persistence
  // effect must observe `false` during the very commit where the hydration
  // effect schedules its setStates — at that point the persistence effect's
  // closure still holds pre-hydration state, and writing it would clobber the
  // stored values with empties. Flipping `hydrated` to true only takes effect
  // on the next render, where the hydrated state is actually committed.
  const [hydrated, setHydrated] = useState(false);
  // Guards the one-time `get_started_completed` emit. Seeded from the persisted
  // `completedFired` flag so a fully-onboarded returning user never re-emits it;
  // re-seeded in the hydration effect below for the hard-reload (null-at-mount)
  // case where `stored` is empty at first render.
  const completedFiredRef = useRef<boolean>(!!stored.completedFired);
  // The last snapshot we persisted. Initialised to the render-time `stored`
  // snapshot so a returning user whose local state matches it produces NO
  // write, while the first-run auto-open (which flips `seen`) DOES diff and
  // write. Re-seeded in the hydration effect for the hard-reload case.
  const lastPersistedRef = useRef<GetStartedMeta>({
    seen: !!stored.seen,
    dismissed: !!stored.dismissed,
    read: stored.read ?? [],
    done: stored.done ?? [],
    completedFired: !!stored.completedFired,
  });
  useEffect(() => {
    if (!isLoaded || hydratedRef.current) return;
    hydratedRef.current = true;
    const s = (user?.unsafeMetadata?.getStarted as GetStartedMeta | undefined) ?? {};
    // Hydrating local state from Clerk's asynchronously-loaded metadata — the
    // external-system→React sync the rule explicitly sanctions. It can't run
    // during render: it must fire when `isLoaded` flips and also dispatches
    // analytics side effects. Runs once (guarded by hydratedRef).
    /* eslint-disable react-hooks/set-state-in-effect */
    setDone(s.done ?? []);
    setRead(s.read ?? []);
    setDismissed(!!s.dismissed);
    setSeen(!!s.seen);
    setCompletedFired(!!s.completedFired);
    completedFiredRef.current = !!s.completedFired;
    // Re-seed the persistence baseline to the freshly-hydrated stored snapshot
    // so this hydration does NOT trigger a write (no diff). The first-run
    // auto-open below flips `seen` to a value that DIFFERS from this baseline,
    // so the persistence effect WILL write it once hydration commits.
    lastPersistedRef.current = {
      seen: !!s.seen,
      dismissed: !!s.dismissed,
      read: s.read ?? [],
      done: s.done ?? [],
      completedFired: !!s.completedFired,
    };
    if (!s.seen) { capture("get_started_opened"); setIsOpen(true); setSeen(true); }
    setHydrated(true);
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [isLoaded, user]);

  // Single persistence path. Writes the COMPLETE `getStarted` object built from
  // current local state, merged into the rest of `unsafeMetadata` so sibling
  // keys (e.g. `onboarded`) survive. Keyed on the persisted-state tuple so it
  // reads COMMITTED state (no stale-closure trap) and naturally coalesces
  // React-batched updates into one write. The `lastPersistedRef` diff makes
  // hydration a no-op while letting every real change write exactly once.
  useEffect(() => {
    if (!hydrated || !user) return;
    const full: GetStartedMeta = { seen, dismissed, read, done, completedFired };
    if (sameSnapshot(lastPersistedRef.current, full)) return;
    lastPersistedRef.current = full;
    // A failed metadata write must not crash the app or surface as an unhandled
    // rejection: report it and swallow (do NOT rethrow). A failed write is
    // retried implicitly on the next state change, when the snapshot diffs
    // again — metadata here is best-effort UI bookkeeping.
    void user.update({ unsafeMetadata: { ...user.unsafeMetadata, getStarted: full } })
      .catch((err: unknown) => { captureException(err); });
  }, [seen, dismissed, read, done, completedFired, hydrated, user]);

  // Event-bus completions (chat/task/hire) → append local state; the
  // persistence effect writes it. `done` is read through a ref so the
  // subscription stays stable, and the state mutation lives OUTSIDE the state
  // updater (updaters must be pure; StrictMode double-invokes them). The ref is
  // synced in an effect (not during render); the onStep handler runs after
  // commit, so it always reads the latest committed value.
  const doneRef = useRef(done);
  useEffect(() => { doneRef.current = done; }, [done]);
  useEffect(() => onStep((id) => {
    // Safe to read-then-append non-atomically: producers (chat send / task
    // create / hire install) are distinct one-shot user actions that never
    // co-fire in the same synchronous tick, so no completion can be dropped.
    const cur = doneRef.current;
    if (cur.includes(id)) return;
    setDone([...cur, id]);
  }), []);

  const d = opts.derived;
  const completed = useMemo<Record<StepId, boolean>>(() => ({
    meet: !!d.meet || read.includes("meet-friday-atlas"),
    chat: !!d.chat || done.includes("chat"),
    task: !!d.task || done.includes("task"),
    hire: !!d.hire || done.includes("hire"),
    channel: !!d.channel || done.includes("channel"),
  }), [read, done, d.meet, d.chat, d.task, d.hire, d.channel]);

  const progress = useMemo(() => ({
    done: STEPS.filter((s) => completed[s.id]).length,
    total: STEPS.length,
  }), [completed]);

  // Fire the completion event exactly once EVER per user (the ref is the
  // StrictMode-safe guard; `completedFired` state is set so the persistence
  // effect writes the flag and it survives reloads).
  useEffect(() => {
    if (completedFiredRef.current) return;
    if (progress.total > 0 && progress.done === progress.total) {
      completedFiredRef.current = true;
      capture("get_started_completed");
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setCompletedFired(true);
    }
  }, [progress.done, progress.total]);

  const openArticle = useCallback((id: ArticleId) => {
    capture("get_started_article_opened", { id });
    setActiveArticle(id);
    // Functional updater: two quick opens must not each read the same stale
    // `read` and drop one append. The de-dupe runs inside the updater against
    // the committed value, so the callback needs no `read` dependency.
    // Persistence is the coalesced effect keyed on `read`; no inline persist.
    setRead((cur) => (cur.includes(id) ? cur : [...cur, id]));
  }, []);

  return {
    isOpen, minimized, tab, activeArticle,
    open: () => { capture("get_started_opened"); setDismissed(false); setIsOpen(true); setMinimized(false); },
    minimize: () => setMinimized(true),
    close: () => setIsOpen(false),
    dismiss: () => { capture("get_started_dismissed"); setDismissed(true); setIsOpen(false); },
    setTab,
    openArticle,
    backToLearn: () => setActiveArticle(null),
    completed, progress, dismissed, seen,
  };
}
