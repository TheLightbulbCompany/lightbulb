import { useEffect } from "react";
import { Sparkles, X, Minus, ArrowRight, ChevronLeft } from "lucide-react";
import { ARTICLES } from "./getStartedContent";
import { GetStartedChecklist } from "./GetStartedChecklist";
import type { StepId } from "./getStartedConfig";
import type { useGetStarted } from "./useGetStarted";

export function GetStartedCard({ gs, onAction }: {
  gs: ReturnType<typeof useGetStarted>;
  onAction: (id: Exclude<StepId, "meet">) => void;
}): React.ReactElement {
  const article = gs.activeArticle ? ARTICLES.find((a) => a.id === gs.activeArticle) : null;
  // Escape closes the card — a baseline expectation for any role="dialog".
  // The card is only mounted while open, so the listener is live exactly when a
  // user could press Escape; it tears down on unmount. Closing here reuses the
  // same handler the X button calls so the two dismiss paths can't diverge.
  const { close } = gs;
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [close]);
  return (
    <section role="dialog" aria-label="Get started" aria-modal={false}
      className="fixed right-5 bottom-[78px] z-50 w-[384px] max-h-[520px] flex flex-col bg-[var(--cream)] border border-[var(--edge)] rounded-2xl shadow-[0_18px_50px_-12px_color-mix(in_srgb,var(--midnight)_28%,transparent)] overflow-hidden">
      <div className="px-4 pt-3.5">
        <div className="flex items-center gap-2.5 mb-3">
          <span className="w-[30px] h-[30px] rounded-lg bg-[var(--midnight)] grid place-items-center"><Sparkles size={16} className="text-[var(--cream)]" /></span>
          <div><div className="font-editorial-serif text-[16.5px] font-semibold leading-tight">Get started</div><div className="text-[11.5px] text-[var(--midnight-muted)]">You’re set up — first wins</div></div>
          <div className="ml-auto flex gap-0.5">
            <button type="button" aria-label="Minimize" onClick={gs.minimize} className="w-7 h-7 rounded-md grid place-items-center text-[var(--midnight-muted)] hover:bg-[var(--cream-deeper)]"><Minus size={16} /></button>
            <button type="button" aria-label="Close" onClick={gs.close} className="w-7 h-7 rounded-md grid place-items-center text-[var(--midnight-muted)] hover:bg-[var(--cream-deeper)]"><X size={16} /></button>
          </div>
        </div>
        <div className="flex gap-5 border-b border-[var(--edge)]">
          {(["do", "learn"] as const).map((t) => (
            <button key={t} type="button" onClick={() => { gs.backToLearn(); gs.setTab(t); }}
              className={`relative py-2.5 text-[13px] font-semibold ${gs.tab === t ? "text-[var(--midnight)] after:absolute after:left-0 after:right-0 after:-bottom-px after:h-0.5 after:bg-[var(--midnight)]" : "text-[var(--midnight-muted)]"}`}>
              {t === "do" ? "Checklist" : "Learn the basics"}
            </button>
          ))}
        </div>
      </div>
      <div className="p-4 overflow-auto">
        {gs.tab === "do" && (
          <GetStartedChecklist completed={gs.completed} onAction={onAction} onMeet={() => { gs.setTab("learn"); gs.openArticle("meet-friday-atlas"); }} onDismiss={gs.dismiss} />
        )}
        {gs.tab === "learn" && !article && (
          <div>
            {ARTICLES.map((a) => (
              <button key={a.id} type="button" onClick={() => gs.openArticle(a.id)}
                className="w-full flex gap-3 items-center text-left p-3.5 border border-[var(--edge)] rounded-xl bg-[var(--cream)] mb-2.5 hover:border-[var(--brass)]">
                <div className="flex-1"><div className="text-sm font-semibold">{a.title}</div><div className="text-xs text-[var(--midnight-muted)]">{a.tag}</div></div>
                <ArrowRight size={16} className="text-[var(--midnight-muted)]" />
              </button>
            ))}
          </div>
        )}
        {gs.tab === "learn" && article && (
          <div>
            <button type="button" onClick={gs.backToLearn} className="inline-flex items-center gap-1.5 text-xs font-semibold text-[var(--midnight)] mb-4"><ChevronLeft size={15} /> All concepts</button>
            <div className="text-[11px] tracking-wider uppercase font-bold text-[var(--midnight-muted)] mb-1.5">{article.tag}</div>
            {/* h2, not h1: this is a floating dialog, not the page's top-level
                heading — nests under the card's "Get started" header. */}
            <h2 className="font-editorial-serif text-[22px] font-semibold leading-tight mb-3">{article.title}</h2>
            {article.body}
          </div>
        )}
      </div>
    </section>
  );
}
