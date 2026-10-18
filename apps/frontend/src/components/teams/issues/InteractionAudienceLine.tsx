// apps/frontend/src/components/teams/issues/InteractionAudienceLine.tsx

// Ported from upstream Paperclip's InteractionAudienceLine
// (paperclip/ui/src/components/InteractionAudienceLine.tsx @ v2026.1012.0-isol8.4)
// (MIT, (c) 2025 Paperclip AI). Markup, the card / compact variants, the
// title-attribute long form, word wrapping and test ids are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - ../lib/interaction-audience -> ./lib/interaction-audience
//   - text-xs / text-muted-foreground / text-foreground -> text-caption,
//     --fg-mute, --fg

import { Users } from "lucide-react";
import { cn } from "@/lib/utils";
import type { InteractionAudienceDescription } from "./lib/interaction-audience";

/**
 * Who may resolve an interaction, stated before anyone acts. The full card uses
 * `variant="card"`; a collapsed attention row above compact Accept/Reject uses
 * `variant="compact"`, which shows the short clause and keeps the long form in
 * the title attribute, so the two surfaces never describe one policy differently.
 *
 * Presentation only: nothing here enables or disables a control.
 */
export function InteractionAudienceLine({
  audience,
  variant = "card",
  className,
}: {
  audience: InteractionAudienceDescription;
  variant?: "card" | "compact";
  className?: string;
}) {
  const compact = variant === "compact";
  return (
    <div
      className={cn(
        "flex items-start gap-2 text-caption leading-5 text-[var(--fg-mute)]",
        compact ? "min-w-0" : "max-w-3xl",
        className,
      )}
      data-testid="interaction-audience"
      data-audience-policy={audience.policy}
      data-audience-open={audience.isOpen ? "true" : "false"}
      data-audience-variant={variant}
      title={compact
        ? [audience.summary, audience.narrowedNote].filter(Boolean).join(" ")
        : undefined}
    >
      <Users className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      {/* An agent name is one unbreakable word; wrapping beats a silent mid-word
          cut that names the wrong responder. */}
      <div className="min-w-0 break-words">
        {compact ? null : (
          <>
            <span className="font-medium text-[var(--fg)]">{audience.label}</span>
            {" — "}
          </>
        )}
        <span data-testid="interaction-audience-summary">
          {compact ? audience.shortSummary : audience.summary}
        </span>
        {!compact && audience.narrowedNote ? (
          <span className="block" data-testid="interaction-audience-note">
            {audience.narrowedNote}
          </span>
        ) : null}
      </div>
    </div>
  );
}
