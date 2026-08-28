import { cn } from "@/lib/utils";

export function Card({
  title,
  action,
  children,
  className,
}: {
  title?: string;
  /** Optional header-right slot (status chip, link) rendered beside the title. */
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("rounded-lg border border-border bg-card p-4", className)}>
      {title || action ? (
        <div className="mb-3 flex items-center justify-between gap-3">
          {title ? <h2 className="text-sm font-semibold text-muted-foreground">{title}</h2> : null}
          {action ?? null}
        </div>
      ) : null}
      {children}
    </div>
  );
}
