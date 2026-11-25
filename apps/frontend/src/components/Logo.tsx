import Link from "next/link";

type LogoTone = "midnight" | "cream";
type LogoSize = "sm" | "md" | "lg";

type LogoProps = {
  tone?: LogoTone;
  size?: LogoSize;
  href?: string | null;
  className?: string;
  ariaLabel?: string;
  /** Render the bulb mark before the wordmark. */
  withMark?: boolean;
};

/*
 * Brand Identity Manual v1.0 §02 (Anatomy).
 *
 * Glass    ST64 — straight sides rising to a single elliptical dome. Filled
 *          bulb-yellow on the product mark, outlined on the holding mark.
 * Filament Three tapered blades, tied twice — an Edison cage. Three, not
 *          four, and the two ties are what stop it reading as a sunburst.
 * Rays     53 needles at 5°, long/short alternating, centred on the GLASS
 *          (not the screw collar), sweeping 260°. Holding company only.
 *
 * These paths are a RECONSTRUCTION from the written spec, not the supplied
 * artwork — the manual's own Open Items note that true vector files do not
 * exist yet. Replace before anything is cut, etched, or printed.
 */
export const GLASS_PATH =
  "M 86,150 L 72,132 L 72,76 A 28,32 0 0 1 128,76 L 128,132 L 114,150 Z";

/** Three tapered blades gathering at the stem, crossed by two ties. */
function Filament({ stroke }: { stroke: string }) {
  return (
    <g stroke={stroke} strokeWidth="3.4" fill={stroke} strokeLinejoin="round">
      <path d="M 87,74 L 95,74 L 101,146 L 99,146 Z" />
      <path d="M 96,74 L 104,74 L 101.4,146 L 98.6,146 Z" />
      <path d="M 105,74 L 113,74 L 101,146 L 99,146 Z" />
      <path d="M 74,96 H 126 M 82,120 H 118" fill="none" />
    </g>
  );
}

function Collar({ fill, tip }: { fill: string; tip: string }) {
  return (
    <g fill={fill}>
      <rect x="86" y="150" width="28" height="7" rx="1" />
      <rect x="87" y="160" width="26" height="7" rx="1" />
      <rect x="89" y="170" width="22" height="6" rx="1" fill={tip} />
    </g>
  );
}

/**
 * The product mark: the lamp with the ceremony removed — no ray fan, no
 * platform, glass filled. Built to survive at 20px in a browser tab, which
 * the holding company's sunrise mark cannot do (§01, §04).
 */
export function LogoMark({ className }: { className?: string }) {
  return (
    <svg
      className={["brand-mark", className].filter(Boolean).join(" ")}
      viewBox="62 40 76 148"
      role="img"
      aria-label="Lightbulb"
      focusable="false"
    >
      <path d={GLASS_PATH} fill="var(--bulb)" stroke="var(--midnight)" strokeWidth="4" />
      <Filament stroke="var(--midnight)" />
      <Collar fill="var(--midnight)" tip="var(--midnight)" />
    </svg>
  );
}

export function Logo({
  tone = "midnight",
  size = "md",
  href = "/",
  className,
  ariaLabel = "Lightbulb home",
  withMark = false,
}: LogoProps) {
  const classes = ["brand-logo", `brand-logo--${tone}`, `brand-logo--${size}`, className]
    .filter(Boolean)
    .join(" ");

  // §10: the product is "Lightbulb". §03: the wordmark is Lora Medium,
  // uppercase, tracked +0.18em — set in .brand-logo, never inline here.
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
