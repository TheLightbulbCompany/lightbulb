import { cn } from "@/lib/utils";

export function Card({ title, children, className }: { title?: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("rounded-lg border border-zinc-800 bg-zinc-900/40 p-4", className)}>
      {title ? <h2 className="mb-3 text-sm font-semibold text-zinc-300">{title}</h2> : null}
      {children}
    </div>
  );
}
