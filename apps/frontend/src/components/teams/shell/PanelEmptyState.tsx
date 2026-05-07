import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { EditorialButton } from "./EditorialButton";

export interface PanelEmptyStateAction {
  label: ReactNode;
  onClick: () => void;
}

export interface PanelEmptyStateProps {
  icon: LucideIcon;
  headline: ReactNode;
  supporting: ReactNode;
  action?: PanelEmptyStateAction;
  className?: string;
}

export function PanelEmptyState({
  icon: Icon,
  headline,
  supporting,
  action,
  className,
}: PanelEmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 py-16 text-center",
        className,
      )}
    >
      <div className="flex h-12 w-12 items-center justify-center rounded-md bg-[var(--edge)]">
        <Icon aria-hidden="true" className="h-6 w-6 text-[var(--mute)]" />
      </div>
      <h2 className="font-editorial-serif text-[22px] italic leading-tight text-[var(--ink)]">
        {headline}
      </h2>
      <p className="max-w-[360px] text-[13px] text-[var(--mute)]">
        {supporting}
      </p>
      {action && (
        <EditorialButton variant="primary" onClick={action.onClick} className="mt-2">
          {action.label}
        </EditorialButton>
      )}
    </div>
  );
}
