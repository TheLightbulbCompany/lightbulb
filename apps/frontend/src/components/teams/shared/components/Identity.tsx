// Ported from upstream Paperclip's Identity.tsx
// (paperclip/ui/src/components/Identity.tsx) (MIT, (c) 2025 Paperclip AI).
// Deviations from upstream: upstream wraps a shadcn <Avatar> built on
// `radix-ui` (the meta-package). The Isol8 frontend ships no shadcn
// avatar.tsx primitive at components/ui/, so we render a minimal
// initials-on-circle avatar inline. Visual behaviour matches the upstream
// fallback path; if a shadcn Avatar primitive ever lands, swap the inline
// span for the real one.

import { cn } from "@/lib/utils";

type IdentitySize = "xs" | "sm" | "default" | "lg";

export interface IdentityProps {
  name: string;
  avatarUrl?: string | null;
  initials?: string;
  size?: IdentitySize;
  className?: string;
}

function deriveInitials(name: string): string {
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  return name.slice(0, 2).toUpperCase();
}

const avatarSize: Record<IdentitySize, string> = {
  xs: "size-5 text-2xs",
  sm: "size-6 text-xs",
  default: "size-8 text-sm",
  lg: "size-10 text-sm",
};

const textSize: Record<IdentitySize, string> = {
  xs: "text-sm",
  sm: "text-xs",
  default: "text-sm",
  lg: "text-sm",
};

export function Identity({
  name,
  avatarUrl,
  initials,
  size = "default",
  className,
}: IdentityProps) {
  const displayInitials = initials ?? deriveInitials(name);

  return (
    <span
      className={cn(
        "inline-flex gap-1.5 items-center",
        size === "xs" && "gap-1",
        size === "lg" && "gap-2",
        className,
      )}
    >
      <span
        className={cn(
          "relative inline-flex shrink-0 select-none items-center justify-center overflow-hidden rounded-full",
          avatarSize[size],
        )}
        style={{
          backgroundColor: "var(--cream-deep)",
          color: "var(--midnight-muted)",
          border: "1px solid var(--edge)",
        }}
        aria-hidden={avatarUrl ? undefined : true}
      >
        {avatarUrl ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={avatarUrl}
            alt={name}
            className="aspect-square size-full object-cover"
          />
        ) : (
          <span>{displayInitials}</span>
        )}
      </span>
      <span
        className={cn("truncate", textSize[size])}
        style={{ color: "var(--midnight)" }}
      >
        {name}
      </span>
    </span>
  );
}
