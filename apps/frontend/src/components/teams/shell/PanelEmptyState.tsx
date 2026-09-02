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
  /** A second, quieter exit beside the primary one. */
  secondaryAction?: PanelEmptyStateAction;
  className?: string;
}

export function PanelEmptyState({
  icon: Icon,
  headline,
  supporting,
  action,
  secondaryAction,
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
        <Icon aria-hidden="true" size={20} className="text-[var(--midnight-muted)]" />
      </div>
      <h2 className="font-editorial-serif text-title italic leading-tight text-[var(--midnight)]">
        {headline}
      </h2>
      <p className="max-w-[360px] text-ui text-[var(--midnight-muted)]">
        {supporting}
      </p>
      {(action || secondaryAction) && (
        <div className="mt-2 flex flex-wrap items-center justify-center gap-2">
          {action && (
            <EditorialButton variant="primary" onClick={action.onClick}>
              {action.label}
            </EditorialButton>
          )}
          {secondaryAction && (
            <EditorialButton variant="secondary" onClick={secondaryAction.onClick}>
              {secondaryAction.label}
            </EditorialButton>
          )}
        </div>
      )}
    </div>
  );
}
