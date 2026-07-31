import { Sparkles } from "lucide-react";

export function GetStartedLauncher({ done, total, pulse, onClick }: {
  done: number; total: number; pulse: boolean; onClick: () => void;
}): React.ReactElement {
  const C = 75.4;
  const offset = C * (1 - (total ? done / total : 0));
  return (
    <button type="button" onClick={onClick} aria-label="Get started"
      className="fixed right-5 bottom-5 z-40 flex items-center gap-2.5 h-[46px] pl-2 pr-4 rounded-full bg-[var(--forest)] text-[var(--cream)] shadow-[0_10px_26px_-8px_rgba(6,64,43,.5)] hover:brightness-110 transition">
      <span className="relative w-[30px] h-[30px]">
        <svg width="30" height="30" className="-rotate-90">
          <circle cx="15" cy="15" r="12" stroke="rgba(255,255,255,.25)" strokeWidth="3" fill="none" />
          {/* SVG presentation attributes don't reliably accept var(), so the
              brand gold (--gold) stays a literal hex here. */}
          <circle cx="15" cy="15" r="12" stroke="var(--gold)" strokeWidth="3" fill="none" strokeLinecap="round" strokeDasharray={C} strokeDashoffset={offset} />
        </svg>
        <span className="absolute inset-0 grid place-items-center text-[9px] font-bold">{done}/{total}</span>
      </span>
      <span className="font-semibold text-[13.5px] flex items-center gap-1.5"><Sparkles size={15} /> Get started</span>
      {pulse && <span className="absolute -top-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-[var(--gold)] animate-ping" />}
    </button>
  );
}
