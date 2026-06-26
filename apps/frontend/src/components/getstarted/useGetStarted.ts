import { useCallback, useEffect, useMemo, useState } from "react";
import { useUser } from "@clerk/nextjs";
import { STEPS, type StepId } from "./getStartedConfig";
import type { ArticleId } from "./getStartedContent";
import { onStep } from "./getStartedEvents";

interface GetStartedMeta { seen?: boolean; dismissed?: boolean; read?: ArticleId[]; done?: StepId[] }

export function useGetStarted(opts: { derived: Partial<Record<StepId, boolean>> }) {
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

  // Event-bus completions (chat/task/hire) → append + persist (idempotent).
  useEffect(() => onStep((id) => {
    setDone((cur) => {
      if (cur.includes(id)) return cur;
      const next = [...cur, id];
      persist({ done: next });
      return next;
    });
  }), [persist]);

  // First-run auto-open, once, after Clerk loads.
  useEffect(() => {
    if (!isLoaded || seen) return;
    setIsOpen(true);
    setSeen(true);
    persist({ seen: true });
  }, [isLoaded, seen, persist]);

  const d = opts.derived;
  const completed = useMemo<Record<StepId, boolean>>(() => ({
    meet: read.includes("meet-friday-atlas"),
    chat: !!d.chat || done.includes("chat"),
    task: !!d.task || done.includes("task"),
    hire: !!d.hire || done.includes("hire"),
    channel: !!d.channel || done.includes("channel"),
  }), [read, done, d.chat, d.task, d.hire, d.channel]);

  const progress = useMemo(() => ({
    done: STEPS.filter((s) => completed[s.id]).length,
    total: STEPS.length,
  }), [completed]);

  const openArticle = useCallback((id: ArticleId) => {
    setActiveArticle(id);
    setRead((cur) => { if (cur.includes(id)) return cur; const next = [...cur, id]; persist({ read: next }); return next; });
  }, [persist]);

  return {
    isOpen, minimized, tab, activeArticle,
    open: () => { setIsOpen(true); setMinimized(false); },
    minimize: () => setMinimized(true),
    close: () => setIsOpen(false),
    dismiss: () => { setDismissed(true); setIsOpen(false); persist({ dismissed: true }); },
    setTab,
    openArticle,
    backToLearn: () => setActiveArticle(null),
    completed, progress, dismissed, seen,
  };
}
