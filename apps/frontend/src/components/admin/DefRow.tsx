interface Props {
  label: string;
  /** String values get a hover `title`; nodes (chips, links, times) render as-is. */
  value: React.ReactNode;
  mono?: boolean;
  href?: string;
}

/**
 * Definition-list row: a label + a value (optionally an external link).
 *
 * Previously redefined inline in `users/[id]/{billing,container,agents/[agent_id]}/page.tsx`
 * with the same body modulo the label-column width — promoted to a shared
 * component so format changes apply once. The width is standardised on
 * `w-32` (the container view's width) across all three call sites; the
 * billing view was `w-36` and the agent-detail view was `w-28`, both
 * accepted as small visual regressions in exchange for the dedup.
 */
export function DefRow({ label, value, mono, href }: Props) {
  const valueCls = mono ? "truncate font-mono text-foreground" : "truncate text-foreground";
  const title = typeof value === "string" ? value : undefined;
  return (
    <div className="flex items-baseline gap-3">
      <dt className="w-32 shrink-0 text-xs uppercase tracking-caps text-muted-foreground">
        {label}
      </dt>
      <dd className="min-w-0 flex-1">
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className={`block ${valueCls} text-info hover:underline`}
            title={title}
          >
            {value}
          </a>
        ) : (
          <span className={valueCls} title={title}>
            {value}
          </span>
        )}
      </dd>
    </div>
  );
}
