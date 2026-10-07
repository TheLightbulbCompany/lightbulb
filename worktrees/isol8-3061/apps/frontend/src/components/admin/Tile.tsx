import Link from "next/link";

import { cn } from "@/lib/utils";

const TONE: Record<string, string> = {
  ok: "border-ok",
  warn: "border-warn",
  error: "border-err",
  info: "border-info",
  unknown: "border-border",
};

export function Tile({
  title,
  label,
  sub,
  href,
  tone = "unknown",
  children,
}: {
  title: string;
  label?: string;
  /** Small truncated mono context line (raw id, root cause, alarm name…). */
  sub?: string;
  /** Internal path renders a Link; absolute URL renders a new-tab anchor. */
  href?: string;
  tone?: "ok" | "warn" | "error" | "info" | "unknown";
  children?: React.ReactNode;
}) {
  const body = (
    <>
      <div className="truncate text-xs uppercase tracking-caps text-muted-foreground" title={title}>
        {title}
      </div>
      {label ? <div className="mt-1 text-lg font-semibold text-foreground">{label}</div> : null}
      {sub ? (
        <div className="mt-1 truncate font-mono text-2xs text-muted-foreground" title={sub}>
          {sub}
        </div>
      ) : null}
      {children}
    </>
  );
  const cls = cn("rounded-lg border bg-card p-3", TONE[tone]);
  if (href) {
    const linkCls = cn(cls, "block hover:bg-accent");
    return /^https?:\/\//.test(href) ? (
      <a href={href} target="_blank" rel="noreferrer" className={linkCls}>
        {body}
      </a>
    ) : (
      <Link href={href} className={linkCls}>
        {body}
      </Link>
    );
  }
  return <div className={cls}>{body}</div>;
}
