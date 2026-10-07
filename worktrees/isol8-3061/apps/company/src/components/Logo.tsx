/**
 * The lamp from the Lightbulb brand mark (apps/frontend/src/components/Logo.tsx)
 * — the only thing this site inherits from the product brand. Strokes are
 * currentColor; the rays are gold.
 */
const GLASS = "M 86,150 L 72,132 L 72,76 A 28,32 0 0 1 128,76 L 128,132 L 114,150 Z";

function Lamp() {
  return (
    <>
      <path d={GLASS} fill="none" stroke="currentColor" strokeWidth="4" />
      <g stroke="currentColor" strokeWidth="3.4" fill="currentColor" strokeLinejoin="round">
        <path d="M 87,74 L 95,74 L 101,146 L 99,146 Z" />
        <path d="M 96,74 L 104,74 L 101.4,146 L 98.6,146 Z" />
        <path d="M 105,74 L 113,74 L 101,146 L 99,146 Z" />
        <path d="M 74,96 H 126 M 82,120 H 118" fill="none" />
      </g>
      <g fill="currentColor">
        <rect x="86" y="150" width="28" height="7" rx="1" />
        <rect x="87" y="160" width="26" height="7" rx="1" />
        <rect x="89" y="170" width="22" height="6" rx="1" />
      </g>
    </>
  );
}

export function LampMark({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="62 40 76 148" aria-hidden="true" focusable="false">
      <Lamp />
    </svg>
  );
}

/** The holding-company mark: the lamp, lit — 53 needles at 5°, sweeping 260°. */
export function SunriseMark({ className }: { className?: string }) {
  const rays: string[] = [];
  for (let i = 0; i < 53; i++) {
    const a = ((-130 + i * 5 - 90) * Math.PI) / 180;
    const outer = i % 2 === 0 ? 96 : 80;
    const nx = Math.cos(a), ny = Math.sin(a);
    rays.push(
      `M ${(100 + nx * 62 - ny * 1.1).toFixed(2)},${(100 + ny * 62 + nx * 1.1).toFixed(2)} ` +
        `L ${(100 + nx * outer).toFixed(2)},${(100 + ny * outer).toFixed(2)} ` +
        `L ${(100 + nx * 62 + ny * 1.1).toFixed(2)},${(100 + ny * 62 - nx * 1.1).toFixed(2)} Z`,
    );
  }
  return (
    <svg className={className} viewBox="0 0 200 200" role="img" aria-label="The Lightbulb Company" focusable="false">
      <g fill="var(--gold)">
        {rays.map((d, i) => (
          <path key={i} d={d} />
        ))}
      </g>
      <Lamp />
      <rect x="46" y="184" width="108" height="2.6" fill="var(--gold)" />
    </svg>
  );
}
