import Link from "next/link";

type LogoTone = "ink" | "cream";
type LogoSize = "sm" | "md" | "lg";

type LogoProps = {
  tone?: LogoTone;
  size?: LogoSize;
  href?: string | null;
  className?: string;
  ariaLabel?: string;
};

export function Logo({
  tone = "ink",
  size = "md",
  href = "/",
  className,
  ariaLabel = "isol8 home",
}: LogoProps) {
  const classes = ["brand-logo", `brand-logo--${tone}`, `brand-logo--${size}`, className]
    .filter(Boolean)
    .join(" ");

  const content = <span className={classes}>ISOL8</span>;

  if (href === null) return content;
  return (
    <Link href={href} aria-label={ariaLabel} className="brand-logo-link">
      {content}
    </Link>
  );
}
