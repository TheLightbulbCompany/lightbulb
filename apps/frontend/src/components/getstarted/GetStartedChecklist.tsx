import { Check, Users, MessageSquare, ListPlus, Store, Plug, ArrowRight } from "lucide-react";
import { STEPS, type StepDef, type StepId } from "./getStartedConfig";

const ICONS = { users: Users, message: MessageSquare, listplus: ListPlus, store: Store, plug: Plug } as const;

export function GetStartedChecklist({ completed, onAction, onMeet, onDismiss }: {
  completed: Record<StepId, boolean>;
  onAction: (id: Exclude<StepId, "meet">) => void;
  onMeet: () => void;
  onDismiss: () => void;
}): React.ReactElement {
  const done = STEPS.filter((s) => completed[s.id]).length;
  return (
    <div>
      <div className="text-[13.5px] leading-relaxed text-[var(--midnight)] bg-[var(--cream-deep)] border border-[var(--edge)] rounded-xl p-3.5 mb-3.5">
        Lightbulb runs like a tiny company. <b>Friday</b> is your CEO — you talk to it, it delegates. <b>Atlas</b> recruits new agents when a job needs a skill you don’t have yet. You’re set up — now put them to work.
      </div>
      <div className="flex items-center gap-2.5 flex-wrap bg-[var(--cream-deep)] border border-dashed border-[var(--edge)] rounded-xl px-3 py-2.5 mb-5 text-xs text-[var(--midnight-muted)]">
        <span className="inline-flex items-center gap-1.5 font-semibold text-[var(--midnight)]"><Check size={13} /> Plan & billing</span>
        <span className="inline-flex items-center gap-1.5 font-semibold text-[var(--midnight)]"><Check size={13} /> AI provider</span>
        <span>set during signup</span>
      </div>
      <div className="flex justify-between mb-1.5"><span className="text-[13px] font-semibold">Your first wins</span><span className="text-xs font-semibold text-[var(--midnight)]">{done} of {STEPS.length}</span></div>
      <div className="h-[7px] rounded-full bg-[var(--cream-deeper)] overflow-hidden mb-4"><i className="block h-full rounded-full bg-gradient-to-r from-[var(--brass)] to-[var(--midnight)]" style={{ width: `${(done / STEPS.length) * 100}%` }} /></div>
      {STEPS.map((s: StepDef) => {
        const Icon = ICONS[s.icon];
        const isDone = completed[s.id];
        return (
          <div key={s.id} className={`flex gap-3 p-3 border rounded-xl mb-2.5 ${isDone ? "bg-[var(--cream-deep)] border-[var(--edge)]" : "bg-[var(--cream)] border-[var(--edge)] hover:border-[var(--brass)]"}`}>
            <span className={`flex-none w-[22px] h-[22px] rounded-full grid place-items-center mt-0.5 ${isDone ? "bg-[var(--midnight)]" : "border-2 border-[var(--midnight-muted)]"}`}>{isDone && <Check size={12} className="text-[var(--cream)]" />}</span>
            <span className="flex-none w-[34px] h-[34px] rounded-lg bg-[var(--cream-deep)] grid place-items-center text-[var(--midnight)]"><Icon size={18} /></span>
            <div className="flex-1 min-w-0">
              <div className={`text-sm font-semibold ${isDone ? "text-[var(--midnight-muted)] line-through" : ""}`}>{s.title}</div>
              <div className="text-xs text-[var(--midnight-muted)] mt-0.5 mb-2 leading-snug">{s.description}</div>
              {!isDone && (s.id === "meet"
                ? <button type="button" onClick={onMeet} className="inline-flex items-center gap-1.5 text-xs font-semibold text-[var(--midnight)] bg-[var(--cream-deep)] border border-[var(--edge)] rounded-lg px-2.5 py-1.5">Read <ArrowRight size={13} /></button>
                : <button type="button" onClick={() => onAction(s.id as Exclude<StepId, "meet">)} className="inline-flex items-center gap-1.5 text-xs font-semibold text-[var(--midnight)] bg-[var(--cream-deep)] border border-[var(--edge)] rounded-lg px-2.5 py-1.5">{s.ctaLabel} <ArrowRight size={13} /></button>)}
            </div>
          </div>
        );
      })}
      <button type="button" onClick={onDismiss} className="block mx-auto mt-3.5 text-xs text-[var(--midnight-muted)] underline underline-offset-2">Dismiss checklist</button>
    </div>
  );
}
