import Link from "next/link";

type LogoTone = "ink" | "cream";
type LogoSize = "sm" | "md" | "lg";

type LogoProps = {
  tone?: LogoTone;
  size?: LogoSize;
  href?: string | null;
  className?: string;
  ariaLabel?: string;
  /** Render the serif 8 mark before the wordmark. */
  withMark?: boolean;
};

/**
 * The brand mark: the Didone serif numeral 8 from og-image.png, rendered as
 * a styled glyph in the site serif stack. Size it with font-size where used.
 */
export function LogoMark({ className }: { className?: string }) {
  return (
    <span
      className={["brand-mark", className].filter(Boolean).join(" ")}
      aria-hidden="true"
    >
      8
    </span>
  );
}

export function Logo({
  tone = "ink",
  size = "md",
  href = "/",
  className,
  ariaLabel = "Lightbulb AI home",
  withMark = false,
}: LogoProps) {
  const classes = ["brand-logo", `brand-logo--${tone}`, `brand-logo--${size}`, className]
    .filter(Boolean)
    .join(" ");

  const content = (
    <span className={classes}>
      {withMark && <LogoMark />}
      LIGHTBULB
    </span>
  );

  if (href === null) return content;
  return (
    <Link href={href} aria-label={ariaLabel} className="brand-logo-link">
      {content}
    </Link>
  );
}
