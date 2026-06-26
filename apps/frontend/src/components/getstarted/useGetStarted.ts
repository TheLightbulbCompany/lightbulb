import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useUser } from "@clerk/nextjs";
import { capture } from "@/lib/analytics";
import { STEPS, type StepId } from "./getStartedConfig";
import type { ArticleId } from "./getStartedContent";
import { onStep } from "./getStartedEvents";

interface GetStartedMeta { seen?: boolean; dismissed?: boolean; read?: ArticleId[]; done?: StepId[] }

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

export function useGetStarted(opts: { derived: Partial<Record<StepId, boolean>> }): UseGetStarted {
  const { isLoaded, user } = useUser();
  const stored = (user?.unsafeMetadata?.getStarted as GetStartedMeta | undefined) ?? {};

  const [done, setDone] = useState<StepId[]>(stored.done ?? []);
  const [read, setRead] = useState<ArticleId[]>(stored.read ?? []);
  const [dismissed, setDismissed] = useState<boolean>(!!stored.dismissed);
  const [seen, setSeen] = useState<boolean>(!!stored.seen);
  const [isOpen, setIsOpen] = useState(false);
  const [minimized, setMinimized] = useState(false);
  const [tab, setTab] = useState<"do" | "learn">("do");
  const [activeArticle, setActiveArticle] = useState<ArticleId | null>(null);

  // Persist a partial patch, MERGING with existing unsafeMetadata so we never
  // clobber the `onboarded` flag or anything else stored there.
  const persist = useCallback((patch: Partial<GetStartedMeta>) => {
    if (!user) return;
    const prev = (user.unsafeMetadata?.getStarted as GetStartedMeta | undefined) ?? {};
    void user.update({ unsafeMetadata: { ...user.unsafeMetadata, getStarted: { ...prev, ...patch } } });
  }, [user]);

  // Hydrate local state from the loaded Clerk metadata exactly once. On a hard
  // page load `useUser()` first returns `{ isLoaded: false, user: null }`, so a
  // returning user's persisted state is not available at mount — without this
  // we would auto-open the guide and reset completion on every reload. The
  // first-run auto-open is folded in here so it respects the loaded `seen`.
  const hydratedRef = useRef(false);
  useEffect(() => {
    if (!isLoaded || hydratedRef.current) return;
    hydratedRef.current = true;
    const s = (user?.unsafeMetadata?.getStarted as GetStartedMeta | undefined) ?? {};
    // Hydrating local state from Clerk's asynchronously-loaded metadata — the
    // external-system→React sync the rule explicitly sanctions. It can't run
    // during render: it must fire when `isLoaded` flips and also dispatches
    // analytics + persist side effects. Runs once (guarded by hydratedRef).
    /* eslint-disable react-hooks/set-state-in-effect */
    setDone(s.done ?? []);
    setRead(s.read ?? []);
    setDismissed(!!s.dismissed);
    setSeen(!!s.seen);
    if (!s.seen) { capture("get_started_opened"); setIsOpen(true); setSeen(true); persist({ seen: true }); }
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [isLoaded, user, persist]);

  // Event-bus completions (chat/task/hire) → append + persist (idempotent).
  // `done` is read through a ref so the subscription stays stable, and the
  // persist side effect lives OUTSIDE the state updater (updaters must be pure;
  // StrictMode double-invokes them). The ref is synced in an effect (not during
  // render — writing a ref during render is disallowed); the onStep handler
  // runs after commit, so it always reads the latest committed value.
  const doneRef = useRef(done);
  useEffect(() => { doneRef.current = done; }, [done]);
  useEffect(() => onStep((id) => {
    const cur = doneRef.current;
    if (cur.includes(id)) return;
    const next = [...cur, id];
    setDone(next);
    persist({ done: next });
  }), [persist]);

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

  // Fire the completion event exactly once, the first time every step is done.
  const completedFiredRef = useRef(false);
  useEffect(() => {
    if (completedFiredRef.current) return;
    if (progress.total > 0 && progress.done === progress.total) {
      completedFiredRef.current = true;
      capture("get_started_completed");
    }
  }, [progress.done, progress.total]);

  const openArticle = useCallback((id: ArticleId) => {
    capture("get_started_article_opened", { id });
    setActiveArticle(id);
    if (read.includes(id)) return;
    const next = [...read, id];
    setRead(next);
    persist({ read: next });
  }, [read, persist]);

  return {
    isOpen, minimized, tab, activeArticle,
    open: () => { capture("get_started_opened"); setIsOpen(true); setMinimized(false); },
    minimize: () => setMinimized(true),
    close: () => setIsOpen(false),
    dismiss: () => { capture("get_started_dismissed"); setDismissed(true); setIsOpen(false); persist({ dismissed: true }); },
    setTab,
    openArticle,
    backToLearn: () => setActiveArticle(null),
    completed, progress, dismissed, seen,
  };
}
