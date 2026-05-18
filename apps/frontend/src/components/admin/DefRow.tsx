interface Props {
  label: string;
  value: string;
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
  const valueCls = mono ? "truncate font-mono text-zinc-200" : "truncate text-zinc-200";
  return (
    <div className="flex items-baseline gap-3">
      <dt className="w-32 shrink-0 text-xs uppercase tracking-wide text-zinc-500">
        {label}
      </dt>
      <dd className="min-w-0 flex-1">
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className={`block ${valueCls} text-sky-300 hover:underline`}
            title={value}
          >
            {value}
          </a>
        ) : (
          <span className={valueCls} title={value}>
            {value}
          </span>
        )}
      </dd>
    </div>
  );
}
