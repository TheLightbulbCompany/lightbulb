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
  return (
    <section role="dialog" aria-label="Get started"
      className="fixed right-5 bottom-[78px] z-50 w-[384px] max-h-[520px] flex flex-col bg-[var(--cream)] border border-[var(--edge)] rounded-2xl shadow-[0_18px_50px_-12px_rgba(6,64,43,.28)] overflow-hidden">
      <div className="px-4 pt-3.5">
        <div className="flex items-center gap-2.5 mb-3">
          <span className="w-[30px] h-[30px] rounded-lg bg-[var(--forest)] grid place-items-center"><Sparkles size={16} className="text-[var(--cream)]" /></span>
          <div><div className="font-editorial-serif text-[16.5px] font-semibold leading-tight">Get started</div><div className="text-[11.5px] text-[var(--mute)]">You’re set up — first wins</div></div>
          <div className="ml-auto flex gap-0.5">
            <button type="button" aria-label="Minimize" onClick={gs.minimize} className="w-7 h-7 rounded-md grid place-items-center text-[var(--mute)] hover:bg-[var(--cream-deepest)]"><Minus size={16} /></button>
            <button type="button" aria-label="Close" onClick={gs.close} className="w-7 h-7 rounded-md grid place-items-center text-[var(--mute)] hover:bg-[var(--cream-deepest)]"><X size={16} /></button>
          </div>
        </div>
        <div className="flex gap-5 border-b border-[var(--edge)]">
          {(["do", "learn"] as const).map((t) => (
            <button key={t} type="button" onClick={() => { gs.backToLearn(); gs.setTab(t); }}
              className={`relative py-2.5 text-[13px] font-semibold ${gs.tab === t ? "text-[var(--forest)] after:absolute after:left-0 after:right-0 after:-bottom-px after:h-0.5 after:bg-[var(--forest)]" : "text-[var(--mute)]"}`}>
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
                className="w-full flex gap-3 items-center text-left p-3.5 border border-[var(--edge)] rounded-xl bg-white mb-2.5 hover:border-[var(--forest-soft)]">
                <div className="flex-1"><div className="text-sm font-semibold">{a.title}</div><div className="text-xs text-[var(--mute)]">{a.tag}</div></div>
                <ArrowRight size={16} className="text-[var(--mute)]" />
              </button>
            ))}
          </div>
        )}
        {gs.tab === "learn" && article && (
          <div>
            <button type="button" onClick={gs.backToLearn} className="inline-flex items-center gap-1.5 text-xs font-semibold text-[var(--forest)] mb-4"><ChevronLeft size={15} /> All concepts</button>
            <div className="text-[11px] tracking-wider uppercase font-bold text-[var(--forest-soft)] mb-1.5">{article.tag}</div>
            <h1 className="font-editorial-serif text-[22px] font-semibold leading-tight mb-3">{article.title}</h1>
            {article.body}
          </div>
        )}
      </div>
    </section>
  );
}
