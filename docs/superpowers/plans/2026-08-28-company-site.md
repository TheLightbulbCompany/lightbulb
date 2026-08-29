# thelightbulb.company Site Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `apps/company`, the one-page holding-company site at `thelightbulb.company`, as a port of the approved prototype — a scroll-driven three.js bulb whose interior morphs per product — with CI, a gated prod deploy, and DNS.

**Architecture:** A new Next.js 16 app in the monorepo with one page. A fixed full-screen R3F `<Canvas frameloop="demand">` renders a bulb built imperatively in plain three.js (`bulb.ts`, near-verbatim from the prototype) and mounted via `<primitive>`; a `useFrame` driver reads a scroll-progress ref (GSAP ScrollTrigger scrub) and applies every 3D value + a few DOM writes (rail, scrub bar, leader label). The scroll timeline is derived from the sections' real `offsetTop`s (`timeline.ts`, pure and unit-tested), so each chapter's shape is fully formed while its section fills the viewport and morphs in the gap.

**Tech Stack:** Next.js 16.2.12, React 19.2.3, three ^0.184.0, @react-three/fiber 9.7.0, @react-three/postprocessing 3.1.1 (+ postprocessing 6.39.4), gsap ^3.15.0, vitest 3, pnpm 9.15 workspace, Vercel CLI in the CDK-generated `deploy.yml`.

**Spec:** `docs/superpowers/specs/2026-08-28-company-site-design.md` (+ the pixel reference `docs/superpowers/specs/2026-08-28-company-site-prototype.html`).

## Global Constraints

- Dark throughout: ground `#0a0a0a` → `#1a1612` on the strike; type stays cream `#e8e2d6`; gold `#f0c34e` is the only accent. No light theme.
- Logo-only inheritance from Lightbulb AI: the lamp SVG paths from `apps/frontend/src/components/Logo.tsx`; nothing else (no tokens, no `landing.css`, no Source Sans/Lora).
- Fonts: Instrument Serif (display), Inter (body), JetBrains Mono (labels) via `next/font/google`.
- The bulb stays whole. The interior morphs: scatter → brain → network → town → growth → gear → brain.
- Timeline derived from section positions at runtime; re-derived on resize and font load.
- No new dependencies beyond what `apps/frontend/package.json` already pins (same versions).
- No Clerk, no middleware, no `@lightbulb/shared`, no Tailwind.
- Demand frameloop: 0 fps at rest. Quality ladder: DPR 1.5 → 1 → bloom off. Point count 24 000 desktop, 12 000 under 640 px.
- No WebGL: the scene is skipped; the page reads as plain HTML. `prefers-reduced-motion`: the scene renders **frozen** at the strike state (lit, assembled brain, no scroll binding, no idle wobble).
- Copy is the prototype's placeholder copy except the tagline **"The business of ideas."**; the footer legal line is exactly `Isol8 Inc., doing business as Lightbulb AI`.
- Every change to `apps/infra/lib/app.ts` is followed by `cd apps/infra && npx cdk synth` and the regenerated `.github/workflows/deploy.yml` is committed in the same PR.
- Work in `.worktrees/company-site` (branch `feat/company-site`); use absolute worktree paths for every edit; `git add <paths>`, never `git add -A`.
- Commit messages end with the session trailer:
  ```
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01URYZum9iamp5d6UVkyiByC
  ```

---

## File structure

```
apps/company/
  package.json                 @lightbulb/company — scripts dev/build/lint/test
  next.config.ts               security headers only
  tsconfig.json                copy of apps/frontend's (paths @/* → src/*)
  eslint.config.mjs            nextVitals + nextTs
  vitest.config.ts             node env, src/**/__tests__/**
  vercel.json                  framework nextjs, iad1, install cmd
  public/favicon.svg           the lamp mark, cream on transparent
  src/app/layout.tsx           fonts + metadata + globals.css
  src/app/globals.css          the prototype's CSS, fonts via CSS vars
  src/app/page.tsx             sections (HTML copy) + <Chrome/> + <LightbulbScene/>
  src/content.ts               CHAPTERS (id, shape) + PRODUCTS copy — one source for page + scene
  src/components/Logo.tsx      LampMark + SunriseMark (SVG paths from the frontend)
  src/components/Chrome.tsx    nav, rail dots, scrub bar, leader label + <svg> (ids the driver writes)
  src/components/LightbulbScene.tsx   client wrapper: WebGL + reduced-motion guard, dynamic import
  src/scene/shapes.ts          makeShapes(n, rand) → Record<ShapeName, Float32Array>   (pure)
  src/scene/timeline.ts        buildTimeline(tops, max) → keys + windows; morph(); helpers (pure)
  src/scene/bulb.ts            buildBulb(colors, n) → { group, glass, collar, filament, cloud, materials } (three, no React)
  src/scene/Scene.tsx          <Canvas> + lights + bloom + <Driver/> (R3F)
  src/scene/webgl.ts           hasWebGL() — copied from the frontend
  src/scene/__tests__/shapes.test.ts
  src/scene/__tests__/timeline.test.ts
.github/workflows/company-ci.yml     PR lint/tsc/test/build, path-filtered
apps/infra/lib/app.ts                DeployCompanyDev + DeployCompanyProd steps
apps/infra/test/deploy-prod-promotion.test.ts   asserts prod-DeployCompanyProd is gated
.github/workflows/deploy.yml         regenerated by cdk synth
```

---

### Task 1: Scaffold `apps/company` and get `next build` green

**Files:**
- Create: `apps/company/package.json`, `apps/company/next.config.ts`, `apps/company/tsconfig.json`, `apps/company/eslint.config.mjs`, `apps/company/vitest.config.ts`, `apps/company/vercel.json`, `apps/company/.gitignore`, `apps/company/src/app/layout.tsx`, `apps/company/src/app/page.tsx`, `apps/company/src/app/globals.css`
- Modify: `pnpm-lock.yaml` (new importer, via `pnpm install`)

**Interfaces:**
- Produces: the `@lightbulb/company` workspace package; CSS custom properties `--font-serif`, `--font-sans`, `--font-mono`, `--ink`, `--paper`, `--gold`, `--ground`, `--fg`, `--fg-muted`, `--rule` that every later component uses.

- [ ] **Step 1: package.json**

```json
{
  "name": "@lightbulb/company",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "next dev --port 3100",
    "build": "next build",
    "start": "next start",
    "lint": "eslint",
    "typecheck": "tsc --noEmit",
    "test": "vitest run"
  },
  "dependencies": {
    "@react-three/fiber": "9.7.0",
    "@react-three/postprocessing": "3.1.1",
    "gsap": "^3.15.0",
    "next": "16.2.12",
    "postprocessing": "6.39.4",
    "react": "19.2.3",
    "react-dom": "19.2.3",
    "three": "^0.184.0"
  },
  "devDependencies": {
    "@types/node": "^20",
    "@types/react": "^19",
    "@types/react-dom": "^19",
    "@types/three": "^0.184.1",
    "eslint": "^9",
    "eslint-config-next": "16.2.12",
    "typescript": "^5",
    "vitest": "^3.2.4"
  }
}
```

- [ ] **Step 2: config files**

`apps/company/next.config.ts`:
```ts
import type { NextConfig } from "next";

const securityHeaders = [
  { key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "SAMEORIGIN" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
];

const nextConfig: NextConfig = {
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
```

`apps/company/tsconfig.json`: copy `apps/frontend/tsconfig.json` verbatim.

`apps/company/eslint.config.mjs`:
```js
import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

export default defineConfig([
  ...nextVitals,
  ...nextTs,
  globalIgnores([".next/**", "out/**", "next-env.d.ts"]),
]);
```

`apps/company/vitest.config.ts`:
```ts
import { defineConfig } from "vitest/config";
import path from "path";

export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/__tests__/**/*.test.ts"],
  },
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
});
```

`apps/company/vercel.json`:
```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "framework": "nextjs",
  "installCommand": "pnpm install --frozen-lockfile",
  "regions": ["iad1"]
}
```

`apps/company/.gitignore`:
```
/.next
/out
next-env.d.ts
.vercel
*.tsbuildinfo
```

- [ ] **Step 3: layout, page, css (placeholder page — the real one lands in Task 5)**

`apps/company/src/app/layout.tsx`:
```tsx
import type { Metadata } from "next";
import { Instrument_Serif, Inter, JetBrains_Mono } from "next/font/google";
import "./globals.css";

const serif = Instrument_Serif({ variable: "--font-serif", subsets: ["latin"], weight: "400", style: ["normal", "italic"] });
const sans = Inter({ variable: "--font-sans", subsets: ["latin"], weight: ["400", "500"] });
const mono = JetBrains_Mono({ variable: "--font-mono", subsets: ["latin"], weight: ["400", "500"] });

const TITLE = "The Lightbulb Company — the business of ideas";
const DESCRIPTION = "A holding company for products that make people more capable. Lightbulb AI, GooseTown, Ventures, Physical AI.";

export const metadata: Metadata = {
  metadataBase: new URL("https://thelightbulb.company"),
  title: TITLE,
  description: DESCRIPTION,
  openGraph: { title: TITLE, description: DESCRIPTION, url: "https://thelightbulb.company", siteName: "The Lightbulb Company", type: "website" },
  icons: { icon: "/favicon.svg" },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${serif.variable} ${sans.variable} ${mono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
```

`apps/company/src/app/page.tsx` (placeholder for this task only):
```tsx
export default function Page() {
  return <main><h1>The business of ideas.</h1></main>;
}
```

`apps/company/src/app/globals.css`: the `<style>` block of `docs/superpowers/specs/2026-08-28-company-site-prototype.html`, with these substitutions:
- `font-family: Inter, system-ui, sans-serif` → `font-family: var(--font-sans), system-ui, sans-serif`
- every `font-family: "JetBrains Mono", monospace` → `font-family: var(--font-mono), monospace`
- every `font-family: "Instrument Serif", Georgia, serif` / `"Instrument Serif", serif` → `font-family: var(--font-serif), Georgia, serif`
- keep the `:root` block, `#gl`, `.nav`, `.rail`, `.scrub`, `#labels`/`.lbl`, `section`, `.eyebrow`, `h1/h2`, `.hero`, `.strike`, `.prod`, `.vision`, `footer`, and the `@media (max-width: 800px)` block exactly.

- [ ] **Step 4: install and build**

Run (worktree root): `pnpm install --offline` — expected: lockfile gains an `apps/company` importer, no network.
Run: `pnpm --filter @lightbulb/company exec tsc --noEmit && pnpm --filter @lightbulb/company run lint && pnpm --filter @lightbulb/company run build`
Expected: all exit 0; build reports route `/` static.

- [ ] **Step 5: Commit**

```bash
git add apps/company pnpm-lock.yaml
git commit -m "feat(company): scaffold the thelightbulb.company Next.js app"
```

---

### Task 2: Pure modules — shapes and timeline (TDD)

**Files:**
- Create: `apps/company/src/content.ts`, `apps/company/src/scene/shapes.ts`, `apps/company/src/scene/timeline.ts`
- Test: `apps/company/src/scene/__tests__/shapes.test.ts`, `apps/company/src/scene/__tests__/timeline.test.ts`

**Interfaces:**
- Produces:
  - `content.ts`: `type ShapeName = "scatter" | "brain" | "network" | "town" | "growth" | "gear"`; `CHAPTERS: ReadonlyArray<{ id: string; shape: ShapeName }>` (7 entries, in page order); `PRODUCTS: ReadonlyArray<{ id: string; index: string; tag: "Live" | "Building" | "Next"; name: string; blurb: string; sub: string; href?: string }>` (4 entries).
  - `shapes.ts`: `makeShapes(n: number, rand?: () => number): Record<ShapeName, Float32Array>` — each array length `n * 3`, centred near the origin, radius ≤ 2.6 for `scatter`, ≤ 0.6 for the rest.
  - `timeline.ts`: `type Key = [p: number, shape: ShapeName]`; `type Windows = { at: number[]; lit: [number, number]; driftIn: [number, number]; driftOut: [number, number]; vision: [number, number]; products: [number, number][] }`; `buildTimeline(tops: number[], max: number): { keys: Key[]; windows: Windows }`; `morph(keys: Key[], shapes: Record<ShapeName, Float32Array>, p: number, out: Float32Array): void`; `clamp01`, `seg(p, [a, b])`, `ease(t)`, `activeProduct(w: Windows, p: number): number` (−1 when none), `nearestChapter(w: Windows, p: number): number`.

- [ ] **Step 1: content.ts**

```ts
export type ShapeName = "scatter" | "brain" | "network" | "town" | "growth" | "gear";

/** Page order. Each chapter's shape is fully formed while its section fills the viewport. */
export const CHAPTERS = [
  { id: "hero", shape: "scatter" },
  { id: "strike", shape: "brain" },
  { id: "ai", shape: "network" },
  { id: "goose", shape: "town" },
  { id: "ventures", shape: "growth" },
  { id: "physical", shape: "gear" },
  { id: "vision", shape: "brain" },
] as const satisfies ReadonlyArray<{ id: string; shape: ShapeName }>;

export const PRODUCTS = [
  { id: "ai", index: "02", tag: "Live", name: "Lightbulb AI", sub: "a team of agents — live", href: "https://flickmeon.ai",
    blurb: "An AI company in a box. Your own team of agents — a CEO, a recruiter, and whoever you hire next — running around the clock." },
  { id: "goose", index: "03", tag: "Building", name: "GooseTown", sub: "a town run by agents — building",
    blurb: "A living world run by agents. A place to watch what a society of minds does when nobody is typing." },
  { id: "ventures", index: "04", tag: "Next", name: "Ventures", sub: "what compounds — next",
    blurb: "Capital and craft for the founders lighting the next room. Small cheques, real help, no theatre." },
  { id: "physical", index: "05", tag: "Next", name: "Physical AI", sub: "ideas that leave the screen — next",
    blurb: "Ideas that leave the screen. Machines that can be handed a task and trusted to do it." },
] as const;
```

- [ ] **Step 2: failing shape tests**

`apps/company/src/scene/__tests__/shapes.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import { makeShapes } from "../shapes";

const N = 2000;
const radius = (a: Float32Array, i: number) => Math.hypot(a[i * 3], a[i * 3 + 1], a[i * 3 + 2]);

describe("makeShapes", () => {
  const shapes = makeShapes(N, mulberry32(7));

  it("produces every shape at n*3 floats with no NaN", () => {
    for (const name of ["scatter", "brain", "network", "town", "growth", "gear"] as const) {
      expect(shapes[name].length).toBe(N * 3);
      expect(shapes[name].some(Number.isNaN)).toBe(false);
    }
  });

  it("keeps the interior shapes inside the glass and the scatter outside it", () => {
    for (const name of ["brain", "network", "town", "growth", "gear"] as const) {
      for (let i = 0; i < N; i++) expect(radius(shapes[name], i)).toBeLessThanOrEqual(0.62);
    }
    let outside = 0;
    for (let i = 0; i < N; i++) if (radius(shapes.scatter, i) > 0.8) outside++;
    expect(outside).toBeGreaterThan(N * 0.7);
  });

  it("splits the brain down the middle", () => {
    for (let i = 0; i < N; i++) expect(Math.abs(shapes.brain[i * 3])).toBeGreaterThanOrEqual(0.028);
  });

  it("is deterministic for a given rand", () => {
    const again = makeShapes(N, mulberry32(7));
    expect(again.town).toEqual(shapes.town);
  });
});

function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
```

- [ ] **Step 3: run — expect FAIL** (`Cannot find module '../shapes'`)

Run: `pnpm --filter @lightbulb/company exec vitest run src/scene/__tests__/shapes.test.ts`

- [ ] **Step 4: shapes.ts**

```ts
import type { ShapeName } from "@/content";

type V3 = { x: number; y: number; z: number };
const v3 = (x: number, y: number, z: number): V3 => ({ x, y, z });
const dist = (a: V3, b: V3) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);

/** Every generator writes n points into a fresh Float32Array(n*3), centred near the origin. */
export function makeShapes(n: number, rand: () => number = Math.random): Record<ShapeName, Float32Array> {
  const rnd = (a: number, b: number) => a + rand() * (b - a);
  const gauss = () => (rand() + rand() + rand() - 1.5) * 0.8;
  const onSphere = () => {
    const u = rnd(-1, 1), ph = rnd(0, Math.PI * 2), s = Math.sqrt(1 - u * u);
    return v3(s * Math.cos(ph), u, s * Math.sin(ph));
  };
  const alloc = () => new Float32Array(n * 3);
  const put = (p: Float32Array, i: number, x: number, y: number, z: number) => { p[i * 3] = x; p[i * 3 + 1] = y; p[i * 3 + 2] = z; };

  const scatter = alloc();
  for (let i = 0; i < n; i++) {
    const r = 2.6 * Math.cbrt(rand()), d = onSphere();
    put(scatter, i, r * d.x, r * d.y * 0.7 + 0.2, r * d.z);
  }

  const brain = alloc();
  {
    const a = 0.46, b = 0.38, c = 0.34; let i = 0;
    while (i < n) {
      const d = onSphere(), x = d.x, y = d.y, z = d.z;
      const f = 0.075 * Math.sin(9 * x + 3 * Math.sin(5 * z)) * Math.cos(8 * y + 2 * Math.cos(7 * x))
              + 0.045 * Math.sin(14 * z + 6 * y) * Math.cos(6 * x);
      const shell = rand() < 0.7 ? 1 : Math.pow(rand(), 0.45);
      const r = (1 + f) * shell, px = x * a * r, py = y * b * r, pz = z * c * r;
      if (Math.abs(px) < 0.028) continue;              // sagittal fissure
      if (py < -0.22 && rand() < 0.55) continue;       // flatter underside
      put(brain, i, px, py, pz); i++;
    }
  }

  const network = alloc();                              // Lightbulb AI — a team of agents, wired together
  {
    const nodes: V3[] = [v3(0, 0, 0)];
    for (let k = 0; k < 14; k++) { const d = onSphere(), r = rnd(0.32, 0.5); nodes.push(v3(r * d.x, r * d.y, r * d.z)); }
    const edges: [number, number][] = [];
    nodes.forEach((nd, i) => {
      nodes.map((m, j) => ({ j, d: dist(nd, m) })).filter((e) => e.j !== i).sort((a, b) => a.d - b.d).slice(0, 2)
        .forEach((e) => edges.push([i, e.j]));
      if (i) edges.push([0, i]);
    });
    for (let i = 0; i < n; i++) {
      if (rand() < 0.35) {
        const nd = nodes[Math.floor(rand() * nodes.length)];
        put(network, i, nd.x + gauss() * 0.045, nd.y + gauss() * 0.045, nd.z + gauss() * 0.045);
      } else {
        const [a, b] = edges[Math.floor(rand() * edges.length)], t = rand();
        put(network, i, nodes[a].x + (nodes[b].x - nodes[a].x) * t + gauss() * 0.006,
                        nodes[a].y + (nodes[b].y - nodes[a].y) * t + gauss() * 0.006,
                        nodes[a].z + (nodes[b].z - nodes[a].z) * t + gauss() * 0.006);
      }
    }
  }

  const town = alloc();                                 // GooseTown — a little skyline
  {
    const G = 9, cell = 0.105, base = -0.36, h: number[] = [];
    for (let x = 0; x < G; x++) for (let z = 0; z < G; z++) {
      const d = Math.hypot(x - 4, z - 4) / 4;
      h.push(Math.max(0.04, rnd(0.08, 0.72) * (1 - d * 0.55)));
    }
    for (let i = 0; i < n; i++) {
      const c = Math.floor(rand() * G * G), x = Math.floor(c / G), z = c % G, onTop = rand() < 0.25;
      put(town, i, (x - 4) * cell + rnd(-0.04, 0.04), base + (onTop ? h[c] : rand() * h[c]), (z - 4) * cell + rnd(-0.04, 0.04));
    }
  }

  const growth = alloc();                               // Ventures — a curve that compounds
  {
    const bars = 8;
    for (let i = 0; i < n; i++) {
      const b = Math.floor(rand() * bars), x = -0.45 + b * (0.9 / (bars - 1));
      const hh = 0.08 + Math.pow(b / (bars - 1), 1.8) * 0.72, edge = rand() < 0.3;
      put(growth, i, x + (edge ? (rand() < 0.5 ? -0.04 : 0.04) : rnd(-0.04, 0.04)),
                     -0.36 + (edge ? rand() * hh : rand() < 0.3 ? hh : rand() * hh), rnd(-0.04, 0.04));
    }
  }

  const gear = alloc();                                 // Physical AI — something that turns
  {
    const teeth = 12;
    for (let i = 0; i < n; i++) {
      const r = rand(); let x: number, y: number, z: number;
      if (r < 0.5) { const a = rnd(0, Math.PI * 2), t = rnd(0, Math.PI * 2), R = 0.4, tr = 0.06; x = (R + tr * Math.cos(t)) * Math.cos(a); y = (R + tr * Math.cos(t)) * Math.sin(a); z = tr * Math.sin(t); }
      else if (r < 0.88) { const k = Math.floor(rand() * teeth), a = (k / teeth) * Math.PI * 2 + rnd(-0.11, 0.11), rr = rnd(0.42, 0.56); x = rr * Math.cos(a); y = rr * Math.sin(a); z = rnd(-0.06, 0.06); }
      else { const a = rnd(0, Math.PI * 2), rr = rnd(0.08, 0.14); x = rr * Math.cos(a); y = rr * Math.sin(a); z = rnd(-0.05, 0.05); }
      put(gear, i, x, y + 0.02, z);
    }
  }

  return { scatter, brain, network, town, growth, gear };
}
```

- [ ] **Step 5: run — expect PASS**

Run: `pnpm --filter @lightbulb/company exec vitest run src/scene/__tests__/shapes.test.ts`

- [ ] **Step 6: failing timeline tests**

`apps/company/src/scene/__tests__/timeline.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import { CHAPTERS } from "@/content";
import { activeProduct, buildTimeline, morph, nearestChapter } from "../timeline";

// Seven 100vh sections + a 60vh footer at a 1000px viewport: tops 0..6000, max = 6600.
const VH = 1000;
const tops = CHAPTERS.map((_, i) => i * VH);
const max = 7 * VH + 0.6 * VH - VH;

describe("buildTimeline", () => {
  const { keys, windows } = buildTimeline(tops, max);

  it("holds each chapter's shape at its section top", () => {
    CHAPTERS.forEach((c, i) => {
      const p = tops[i] / max;
      const key = keys.find(([kp, shape]) => Math.abs(kp - p) < 1e-9 && shape === c.shape);
      expect(key, `${c.id} @ ${p}`).toBeDefined();
    });
  });

  it("morphs only in the gap between two sections", () => {
    // hold key sits 38% into the gap; the next key is the next section top
    const hold = keys.find(([p, s]) => s === "brain" && p > tops[1] / max)!;
    expect(hold[0]).toBeCloseTo((tops[1] + (tops[2] - tops[1]) * 0.38) / max, 9);
  });

  it("lights the bulb before the strike section arrives", () => {
    expect(windows.lit[0]).toBeGreaterThan(0);
    expect(windows.lit[1]).toBeCloseTo(tops[1] / max, 9);
  });

  it("sinks the bulb before the vision copy lands", () => {
    expect(windows.vision[1]).toBeLessThan(tops[6] / max);
    expect(windows.vision[0]).toBeGreaterThan(tops[5] / max);
  });

  it("maps scroll to the active product and the nearest chapter", () => {
    expect(activeProduct(windows, tops[2] / max)).toBe(0);
    expect(activeProduct(windows, tops[5] / max)).toBe(3);
    expect(activeProduct(windows, 0)).toBe(-1);
    expect(activeProduct(windows, 1)).toBe(-1);
    expect(nearestChapter(windows, 0)).toBe(0);
    expect(nearestChapter(windows, 1)).toBe(6);
    expect(nearestChapter(windows, tops[3] / max + 0.01)).toBe(3);
  });
});

describe("morph", () => {
  const { keys } = buildTimeline(tops, max);
  const shapes = {
    scatter: new Float32Array([0, 0, 0]), brain: new Float32Array([1, 1, 1]), network: new Float32Array([2, 2, 2]),
    town: new Float32Array([3, 3, 3]), growth: new Float32Array([4, 4, 4]), gear: new Float32Array([5, 5, 5]),
  };
  const out = new Float32Array(3);

  it("is exactly the chapter's shape at the section top", () => {
    morph(keys, shapes, tops[2] / max, out);
    expect(Array.from(out)).toEqual([2, 2, 2]);
  });

  it("is between shapes mid-gap and clamps at the ends", () => {
    const mid = (tops[1] + (tops[2] - tops[1]) * 0.7) / max;
    morph(keys, shapes, mid, out);
    expect(out[0]).toBeGreaterThan(1); expect(out[0]).toBeLessThan(2);
    morph(keys, shapes, 1.5, out);
    expect(Array.from(out)).toEqual([1, 1, 1]);
    morph(keys, shapes, -1, out);
    expect(Array.from(out)).toEqual([0, 0, 0]);
  });
});
```

- [ ] **Step 7: run — expect FAIL** (`Cannot find module '../timeline'`)

- [ ] **Step 8: timeline.ts**

```ts
import { CHAPTERS, type ShapeName } from "@/content";

export type Key = [p: number, shape: ShapeName];
export type Windows = {
  at: number[];
  lit: [number, number];
  driftIn: [number, number];
  driftOut: [number, number];
  vision: [number, number];
  products: [number, number][];
};

export const clamp01 = (v: number) => Math.min(1, Math.max(0, v));
export const seg = (p: number, [a, b]: readonly [number, number]) => clamp01((p - a) / (b - a));
export const ease = (t: number) => t * t * (3 - 2 * t);

/** Fraction of the gap between two sections that the previous shape holds before morphing. */
const HOLD = 0.38;

/**
 * The timeline is read off the page: `tops` are the chapters' offsetTop in page
 * order and `max` is the scrollable height, both in px. Every value out is in
 * scroll-progress units (0..1).
 */
export function buildTimeline(tops: number[], max: number): { keys: Key[]; windows: Windows } {
  const at = tops.map((t) => t / max);
  const gap = (i: number) => at[i + 1] - at[i];
  const keys: Key[] = [];
  CHAPTERS.forEach((c, i) => {
    keys.push([at[i], c.shape]);
    if (i < CHAPTERS.length - 1) keys.push([at[i] + gap(i) * HOLD, c.shape]);
  });
  keys.push([1, CHAPTERS[CHAPTERS.length - 1].shape]);
  const windows: Windows = {
    at,
    lit: [at[0] + gap(0) * 0.3, at[1]],
    driftIn: [at[1] + gap(1) * 0.3, at[2]],
    driftOut: [at[5] + gap(5) * 0.25, at[5] + gap(5) * 0.7],
    vision: [at[5] + gap(5) * 0.3, at[5] + gap(5) * 0.8],
    products: [2, 3, 4, 5].map((i) => [at[i] - gap(i - 1) * 0.5, at[i] + gap(i) * 0.5]),
  };
  return { keys, windows };
}

/** Writes the cloud for progress `p` into `out` — a held shape, or a lerp between two. */
export function morph(keys: Key[], shapes: Record<ShapeName, Float32Array>, p: number, out: Float32Array): void {
  let k = 0;
  while (k < keys.length - 2 && p >= keys[k + 1][0]) k++;
  const [pa, sa] = keys[k], [pb, sb] = keys[k + 1];
  const A = shapes[sa], B = shapes[sb];
  if (sa === sb || p <= pa) { out.set(A); return; }
  if (p >= pb) { out.set(B); return; }
  const t = ease((p - pa) / (pb - pa));
  for (let i = 0; i < out.length; i++) out[i] = A[i] + (B[i] - A[i]) * t;
}

export function activeProduct(w: Windows, p: number): number {
  return w.products.findIndex(([a, b]) => p >= a && p < b);
}

export function nearestChapter(w: Windows, p: number): number {
  let best = 0;
  w.at.forEach((a, i) => { if (Math.abs(p - a) < Math.abs(p - w.at[best])) best = i; });
  return best;
}
```

- [ ] **Step 9: run — expect PASS**

Run: `pnpm --filter @lightbulb/company run test`
Expected: 2 files, all tests pass.

- [ ] **Step 10: Commit**

```bash
git add apps/company/src/content.ts apps/company/src/scene
git commit -m "feat(company): interior shapes and the page-derived scroll timeline"
```

---

### Task 3: The bulb and the scene (R3F, demand frameloop)

**Files:**
- Create: `apps/company/src/scene/webgl.ts` (copy `apps/frontend/src/components/landing/HeroLamp/webgl.ts` verbatim), `apps/company/src/scene/bulb.ts`, `apps/company/src/scene/Scene.tsx`, `apps/company/src/components/LightbulbScene.tsx`

**Interfaces:**
- Consumes: `makeShapes`, `buildTimeline`, `morph`, `seg`, `ease`, `activeProduct`, `nearestChapter`, `CHAPTERS`, `PRODUCTS`.
- Produces: `buildBulb(n: number): Bulb` where `Bulb = { group: THREE.Group; glass: THREE.Mesh; collar: THREE.Group; filament: THREE.Group; cloud: THREE.Points; filMat: THREE.MeshStandardMaterial; cloudMat: THREE.PointsMaterial; cloudPos: Float32Array }`; `<Scene />` (client component, no props); `<LightbulbScene />` (the only thing `page.tsx` renders for 3D). Chrome DOM contract (Task 4 must render these ids): `#rail i` ×7, `#scrubDot`, `#lbl`, `#lblName`, `#lblSub`, `#leaders` (an `<svg>`).

- [ ] **Step 1: bulb.ts — the prototype's geometry, no React**

```ts
import * as THREE from "three";

export const INK = new THREE.Color("#0a0a0a");
export const WARM = new THREE.Color("#1a1612");
export const GOLD = new THREE.Color("#f0c34e");
const BRASS = "#a8875a";

export const FLOOR_Y = -1.95;
/** The collar tip is at local y = -1.7, so the bulb rests on the floor at this height. */
export const BULB_REST_Y = FLOOR_Y + 1.7;
export const CLOUD_Y = 0.22;

export type Bulb = {
  group: THREE.Group;
  glass: THREE.Mesh;
  collar: THREE.Group;
  filament: THREE.Group;
  cloud: THREE.Points;
  filMat: THREE.MeshStandardMaterial;
  cloudMat: THREE.PointsMaterial;
  cloudPos: Float32Array;
};

/** ST64 lathed from the logo's glass path; brass collar; three-blade Edison cage; the idea as points. */
export function buildBulb(n: number): Bulb {
  const group = new THREE.Group();

  const profile = [new THREE.Vector2(0.36, -1.02), new THREE.Vector2(0.72, -0.56), new THREE.Vector2(0.72, 0.84)];
  for (let i = 1; i <= 24; i++) {
    const a = (i / 24) * Math.PI / 2;
    profile.push(new THREE.Vector2(0.72 * Math.cos(a), 0.84 + 0.82 * Math.sin(a)));
  }
  const glassMat = new THREE.MeshPhysicalMaterial({
    color: "#ffffff", transmission: 1, thickness: 0.5, roughness: 0.06, ior: 1.5,
    transparent: true, side: THREE.FrontSide, clearcoat: 1, clearcoatRoughness: 0.05,
    // The glass must not write depth or it culls the points inside it.
    depthWrite: false,
  });
  const glass = new THREE.Mesh(new THREE.LatheGeometry(profile, 128), glassMat);
  glass.renderOrder = 2;
  group.add(glass);

  const brassMat = new THREE.MeshStandardMaterial({ color: BRASS, metalness: 1, roughness: 0.32 });
  const collar = new THREE.Group();
  const neck = new THREE.Mesh(new THREE.CylinderGeometry(0.36, 0.34, 0.16, 64), brassMat); neck.position.y = -1.1; collar.add(neck);
  for (let i = 0; i < 4; i++) {
    const ring = new THREE.Mesh(new THREE.TorusGeometry(0.33, 0.045, 16, 64), brassMat);
    ring.rotation.x = Math.PI / 2; ring.position.y = -1.22 - i * 0.11; collar.add(ring);
  }
  const core = new THREE.Mesh(new THREE.CylinderGeometry(0.3, 0.26, 0.5, 64), brassMat); core.position.y = -1.4; collar.add(core);
  const tip = new THREE.Mesh(new THREE.SphereGeometry(0.11, 32, 16), new THREE.MeshStandardMaterial({ color: "#2a2622", metalness: 0.6, roughness: 0.5 }));
  tip.position.y = -1.7; collar.add(tip);
  group.add(collar);

  const filament = new THREE.Group();
  const filMat = new THREE.MeshStandardMaterial({ color: "#2b1e0c", emissive: GOLD, emissiveIntensity: 0, roughness: 0.6, metalness: 0.2 });
  for (let i = 0; i < 3; i++) {
    const a = (i / 3) * Math.PI * 2, dir = new THREE.Vector3(Math.cos(a), 0, Math.sin(a));
    const pts = [
      new THREE.Vector3(0, -0.88, 0).addScaledVector(dir, 0.05),
      new THREE.Vector3(0, -0.45, 0).addScaledVector(dir, 0.42),
      new THREE.Vector3(0, 0.25, 0).addScaledVector(dir, 0.55),
      new THREE.Vector3(0, 0.85, 0).addScaledVector(dir, 0.32),
      new THREE.Vector3(0, 1.12, 0).addScaledVector(dir, 0.02),
    ];
    filament.add(new THREE.Mesh(new THREE.TubeGeometry(new THREE.CatmullRomCurve3(pts, false, "catmullrom", 0.6), 80, 0.011, 8, false), filMat));
  }
  for (const y of [-0.15, 0.6]) {
    const tie = new THREE.Mesh(new THREE.TorusGeometry(y < 0 ? 0.49 : 0.44, 0.006, 8, 96), filMat);
    tie.rotation.x = Math.PI / 2; tie.position.y = y; filament.add(tie);
  }
  const stem = new THREE.Mesh(new THREE.CylinderGeometry(0.04, 0.05, 0.4, 24), glassMat); stem.position.y = -0.95; filament.add(stem);
  group.add(filament);

  const cloudPos = new Float32Array(n * 3);
  const cloudGeo = new THREE.BufferGeometry();
  cloudGeo.setAttribute("position", new THREE.BufferAttribute(cloudPos, 3));
  const cloudMat = new THREE.PointsMaterial({
    color: GOLD.clone().multiplyScalar(1.15), size: 0.014, sizeAttenuation: true,
    transparent: true, opacity: 0.06, blending: THREE.AdditiveBlending, depthWrite: false,
  });
  cloudMat.toneMapped = false;
  const cloud = new THREE.Points(cloudGeo, cloudMat);
  cloud.position.y = CLOUD_Y;
  cloud.renderOrder = 3;
  group.add(cloud);

  return { group, glass, collar, filament, cloud, filMat, cloudMat, cloudPos };
}

export function buildFloor(): THREE.Mesh {
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(40, 40),
    new THREE.MeshStandardMaterial({ color: "#15120f", roughness: 0.85, metalness: 0, transparent: true, opacity: 0.72 }),
  );
  floor.rotation.x = -Math.PI / 2; floor.position.y = FLOOR_Y; floor.renderOrder = -1;
  return floor;
}
```

- [ ] **Step 2: Scene.tsx**

```tsx
"use client";

import * as React from "react";
import * as THREE from "three";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Bloom, EffectComposer } from "@react-three/postprocessing";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { gsap } from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { CHAPTERS, PRODUCTS } from "@/content";
import { makeShapes } from "./shapes";
import { activeProduct, buildTimeline, ease, morph, nearestChapter, seg, type Key, type Windows } from "./timeline";
import type { BloomEffect } from "postprocessing";
import { BULB_REST_Y, CLOUD_Y, FLOOR_Y, GOLD, INK, WARM, buildBulb, buildFloor, type Bulb } from "./bulb";

gsap.registerPlugin(ScrollTrigger);

const CAMERA_Z = 8.1;
const FOV = 34;
const ROT_TURNS = 0.8;
const BLOOM_DARK = 0.9;
const BLOOM_LIT = 0.45;
/** After a scroll/resize the demand loop keeps drawing until the scrub tween settles. */
const SETTLE_EPS = 1e-4;

type Quality = 2 | 1; // 2 = bloom, 1 = no bloom

/** DOM the driver writes each frame. Rendered by <Chrome/>; looked up once on mount. */
function chromeEls() {
  const q = <T extends Element>(sel: string) => document.querySelector<T>(sel);
  return {
    rail: Array.from(document.querySelectorAll<HTMLElement>("#rail i")),
    scrub: q<HTMLElement>("#scrubDot"),
    lbl: q<HTMLElement>("#lbl"),
    lblName: q<HTMLElement>("#lblName"),
    lblSub: q<HTMLElement>("#lblSub"),
    leaders: q<SVGSVGElement>("#leaders"),
  };
}

type DriverProps = {
  bulb: Bulb;
  mirror: THREE.Group;
  n: number;
  /** prefers-reduced-motion: hold the strike state, bind nothing to scroll. */
  frozen: boolean;
  bloomRef: React.RefObject<BloomEffect | null>;
  onQualityDecline: () => void;
};

function Driver({ bulb, mirror, n, frozen, bloomRef, onQualityDecline }: DriverProps) {
  const invalidate = useThree((s) => s.invalidate);
  const camera = useThree((s) => s.camera);
  const scene = useThree((s) => s.scene);
  const gl = useThree((s) => s.gl);
  const shapes = React.useMemo(() => makeShapes(n), [n]);
  const timeline = React.useRef<{ keys: Key[]; windows: Windows } | null>(null);
  const progress = React.useRef(0);
  const lastApplied = React.useRef(-1);
  const els = React.useRef<ReturnType<typeof chromeEls> | null>(null);
  const light = React.useRef<THREE.PointLight>(null);
  const key = React.useRef<THREE.DirectionalLight>(null);
  const slow = React.useRef(0);
  const tmp = React.useMemo(() => new THREE.Vector3(), []);
  const groundC = React.useMemo(() => new THREE.Color(), []);

  React.useEffect(() => {
    els.current = chromeEls();
    const pmrem = new THREE.PMREMGenerator(gl);
    scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    scene.environmentIntensity = 0.25;
    scene.background = INK.clone();
    scene.fog = new THREE.Fog(INK.clone(), 9, 16);

    const layout = () => {
      const max = document.documentElement.scrollHeight - innerHeight;
      const tops = CHAPTERS.map((c) => document.getElementById(c.id)?.offsetTop ?? 0);
      timeline.current = buildTimeline(tops, max);
      if (frozen) progress.current = timeline.current.windows.at[1];   // the strike, held
      invalidate();
    };
    layout();
    document.fonts?.ready.then(layout);
    addEventListener("resize", layout);
    // A fresh load sometimes paints nothing until something asks for a frame;
    // the environment cubemap settles a few frames after creation.
    const kick = window.setTimeout(invalidate, 300);
    const st = frozen ? null : ScrollTrigger.create({
      trigger: document.body, start: "top top", end: "bottom bottom", scrub: 0.8,
      onUpdate: (self) => { progress.current = self.progress; invalidate(); },
    });
    return () => { removeEventListener("resize", layout); window.clearTimeout(kick); st?.kill(); pmrem.dispose(); };
  }, [scene, gl, invalidate, frozen]);

  useFrame((state, delta) => {
    const tl = timeline.current;
    if (!tl) return;
    const p = progress.current, t = frozen ? 0 : state.clock.elapsedTime, W = tl.windows;
    const lit = ease(seg(p, W.lit));
    const vis = ease(seg(p, W.vision));
    const drift = ease(seg(p, W.driftIn)) * (1 - ease(seg(p, W.driftOut)));

    groundC.copy(INK).lerp(WARM, lit);
    (scene.background as THREE.Color).copy(groundC);
    (scene.fog as THREE.Fog).color.copy(groundC);
    document.documentElement.style.setProperty("--ground", `#${groundC.getHexString()}`);

    const g = bulb.group;
    g.rotation.y = p * Math.PI * 2 * ROT_TURNS + Math.sin(t * 0.25) * 0.04;
    g.rotation.z = Math.sin(t * 0.4) * 0.008;
    g.position.set(drift * 1.2, BULB_REST_Y, 0);
    mirror.rotation.copy(g.rotation);
    mirror.position.set(g.position.x, 2 * FLOOR_Y - g.position.y, 0);
    mirror.scale.set(1, -1, 1);

    light.current?.position.set(g.position.x, g.position.y + 0.2, 0);
    if (light.current) light.current.intensity = lit * 3.2;
    if (key.current) key.current.intensity = lit * 0.35;
    bulb.filMat.emissiveIntensity = lit * 1.5;
    scene.environmentIntensity = 0.25 + lit * 0.35;
    if (bloomRef.current) bloomRef.current.intensity = BLOOM_DARK + (BLOOM_LIT - BLOOM_DARK) * lit;
    bulb.cloudMat.opacity = 0.12 + lit * 0.5;
    bulb.cloudMat.size = 0.012 + lit * 0.004;
    bulb.cloud.rotation.y = -g.rotation.y + Math.sin(t * 0.3) * 0.14;   // the idea faces you while the glass turns
    morph(tl.keys, shapes, p, bulb.cloudPos);
    (bulb.cloud.geometry.attributes.position as THREE.BufferAttribute).needsUpdate = true;

    camera.position.set(0, 0.5 + vis * 0.6, CAMERA_Z + vis * 2.2);
    camera.lookAt(g.position.x * 0.7, -0.3 + vis * 3.3, 0);   // look above the bulb: it sinks under the copy

    const e = els.current;
    if (e) {
      const active = activeProduct(W, p);
      if (active >= 0 && e.lbl && e.leaders) {
        tmp.set(0, CLOUD_Y + 0.1, 0); g.localToWorld(tmp); tmp.x += 0.62; tmp.project(camera);
        const x = (tmp.x + 1) / 2 * innerWidth, y = (1 - tmp.y) / 2 * innerHeight;
        const lx = innerWidth * 0.78, ly = innerHeight * 0.36;
        if (e.lblName) e.lblName.textContent = PRODUCTS[active].name;
        if (e.lblSub) e.lblSub.textContent = PRODUCTS[active].sub;
        e.lbl.style.left = `${lx}px`; e.lbl.style.top = `${ly}px`; e.lbl.style.opacity = String(drift);
        e.leaders.innerHTML = `<g style="opacity:${drift}"><polyline points="${x},${y} ${lx - 70},${ly} ${lx - 12},${ly}"/><circle cx="${x}" cy="${y}" r="2.5"/></g>`;
      } else if (e.lbl && e.leaders) { e.lbl.style.opacity = "0"; e.leaders.innerHTML = ""; }
      if (e.scrub) e.scrub.style.left = `${p * 100}%`;
      const chapter = nearestChapter(W, p);
      e.rail.forEach((d, i) => d.classList.toggle("on", i === chapter));
    }

    // Demand loop: keep drawing while the scrub is still moving, then stop.
    if (!frozen && Math.abs(p - lastApplied.current) > SETTLE_EPS) { lastApplied.current = p; invalidate(); }

    // Quality ladder: a sustained slow frame rate drops a rung (never climbs back).
    if (delta > 1 / 30) { if (++slow.current > 30) { slow.current = 0; onQualityDecline(); } } else slow.current = 0;
  });

  return (
    <>
      <pointLight ref={light} color={GOLD} intensity={0} distance={14} decay={1.6} />
      <directionalLight ref={key} color="#ffffff" intensity={0} position={[-3, 4, 5]} />
    </>
  );
}

export default function Scene({ frozen = false }: { frozen?: boolean }) {
  const narrow = typeof window !== "undefined" && window.innerWidth < 640;
  const n = narrow ? 12_000 : 24_000;
  const bulb = React.useMemo(() => buildBulb(n), [n]);
  const mirror = React.useMemo(() => { const m = bulb.group.clone(); m.traverse((o) => { o.renderOrder = -2; }); return m; }, [bulb]);
  const floor = React.useMemo(() => buildFloor(), []);
  const [quality, setQuality] = React.useState<Quality>(2);
  const [dpr, setDpr] = React.useState<[number, number]>([1, 1.5]);
  const bloomRef = React.useRef<BloomEffect | null>(null);
  const decline = React.useCallback(() => {
    if (dpr[1] > 1) setDpr([1, 1]); else setQuality(1);
  }, [dpr]);

  return (
    <div id="gl" aria-hidden="true">
      <Canvas
        frameloop="demand"
        dpr={dpr}
        gl={{ antialias: true, powerPreference: "high-performance", toneMapping: THREE.ACESFilmicToneMapping }}
        camera={{ position: [0, 0.5, CAMERA_Z], fov: FOV, near: 0.1, far: 50 }}
      >
        <primitive object={bulb.group} />
        <primitive object={mirror} />
        <primitive object={floor} />
        <Driver bulb={bulb} mirror={mirror} n={n} frozen={frozen} bloomRef={bloomRef} onQualityDecline={decline} />
        {quality === 2 && (
          <EffectComposer multisampling={narrow ? 0 : 4}>
            <Bloom ref={bloomRef} mipmapBlur luminanceThreshold={1.0} intensity={BLOOM_DARK} radius={0.6} />
          </EffectComposer>
        )}
      </Canvas>
    </div>
  );
}
```

Note: `three/examples/jsm/environments/RoomEnvironment.js` is the import path in three 0.184 (`three/addons/` exists only via import maps).

- [ ] **Step 3: LightbulbScene.tsx (the guard)**

```tsx
"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { hasWebGL } from "@/scene/webgl";

const Scene = dynamic(() => import("@/scene/Scene"), { ssr: false, loading: () => null });

type Mode = "pending" | "scene" | "frozen" | "off";

/** The 3D bulb — scroll-driven, or frozen at the strike under
 *  prefers-reduced-motion. Without WebGL there is no scene: the page is the
 *  HTML copy on the dark ground, which reads on its own. */
export function LightbulbScene() {
  const [mode, setMode] = React.useState<Mode>("pending");
  React.useEffect(() => {
    if (!hasWebGL()) { setMode("off"); return; }
    setMode(window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "frozen" : "scene");
  }, []);
  if (mode === "scene") return <Scene />;
  if (mode === "frozen") return <Scene frozen />;
  return null;
}
```

- [ ] **Step 4: typecheck + lint**

Run: `pnpm --filter @lightbulb/company exec tsc --noEmit && pnpm --filter @lightbulb/company run lint`
Expected: exit 0 (fix any unused-var findings from the placeholder replacements above).

- [ ] **Step 5: Commit**

```bash
git add apps/company/src/scene apps/company/src/components/LightbulbScene.tsx
git commit -m "feat(company): the bulb, the morphing idea inside it, and the demand-loop scene"
```

---

### Task 4: Page, chrome, logo — then look at it

**Files:**
- Create: `apps/company/src/components/Logo.tsx`, `apps/company/src/components/Chrome.tsx`, `apps/company/public/favicon.svg`
- Modify: `apps/company/src/app/page.tsx` (replace the placeholder)

**Interfaces:**
- Consumes: `PRODUCTS`, `<LightbulbScene/>`, the chrome ids from Task 3.
- Produces: the finished page.

- [ ] **Step 1: Logo.tsx — the lamp mark, standalone**

```tsx
/** The lamp from the Lightbulb brand mark (apps/frontend/src/components/Logo.tsx) —
 *  the only thing this site inherits from the product brand. Strokes are
 *  currentColor; the rays are gold. */
const GLASS = "M 86,150 L 72,132 L 72,76 A 28,32 0 0 1 128,76 L 128,132 L 114,150 Z";

function Lamp({ glassFill }: { glassFill: string }) {
  return (
    <>
      <path d={GLASS} fill={glassFill} stroke="currentColor" strokeWidth="4" />
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
      <Lamp glassFill="none" />
    </svg>
  );
}

/** The holding-company mark: the lamp, lit — 53 needles at 5°, sweeping 260°. */
export function SunriseMark({ className }: { className?: string }) {
  const rays: string[] = [];
  for (let i = 0; i < 53; i++) {
    const a = ((-130 + i * 5 - 90) * Math.PI) / 180, outer = i % 2 === 0 ? 96 : 80;
    const nx = Math.cos(a), ny = Math.sin(a);
    rays.push(`M ${(100 + nx * 62 - ny * 1.1).toFixed(2)},${(100 + ny * 62 + nx * 1.1).toFixed(2)} L ${(100 + nx * outer).toFixed(2)},${(100 + ny * outer).toFixed(2)} L ${(100 + nx * 62 + ny * 1.1).toFixed(2)},${(100 + ny * 62 - nx * 1.1).toFixed(2)} Z`);
  }
  return (
    <svg className={className} viewBox="0 0 200 200" role="img" aria-label="The Lightbulb Company" focusable="false">
      <g fill="var(--gold)">{rays.map((d, i) => <path key={i} d={d} />)}</g>
      <Lamp glassFill="none" />
      <rect x="46" y="184" width="108" height="2.6" fill="var(--gold)" />
    </svg>
  );
}
```

`apps/company/public/favicon.svg`: the `LampMark` SVG with `currentColor` replaced by `#e8e2d6`, as a static file.

- [ ] **Step 2: Chrome.tsx**

```tsx
import { CHAPTERS, PRODUCTS } from "@/content";
import { LampMark } from "./Logo";

export function Chrome() {
  return (
    <>
      <nav className="nav">
        <a className="brand" href="#hero"><LampMark /><span>The Lightbulb Company</span></a>
        <div className="links">
          {PRODUCTS.map((p) => <a key={p.id} href={`#${p.id}`}>{p.name}</a>)}
          <a href="mailto:hello@thelightbulb.company">Contact</a>
        </div>
      </nav>
      <div className="rail" id="rail">{CHAPTERS.map((c, i) => <i key={c.id} className={i === 0 ? "on" : undefined} />)}</div>
      <div className="scrub"><b id="scrubDot" /></div>
      <div id="labels">
        <svg id="leaders" />
        <div className="lbl" id="lbl"><span id="lblName" /><small id="lblSub" /></div>
      </div>
    </>
  );
}
```

CSS delta in `globals.css`: `.nav .brand` also gets `color: inherit; text-decoration: none;`.

- [ ] **Step 3: page.tsx**

```tsx
import { PRODUCTS } from "@/content";
import { Chrome } from "@/components/Chrome";
import { LightbulbScene } from "@/components/LightbulbScene";
import { SunriseMark } from "@/components/Logo";

export default function Page() {
  return (
    <>
      <LightbulbScene />
      <Chrome />
      <main>
        <section className="hero" id="hero">
          <p className="eyebrow">The Lightbulb Company</p>
          <h1>The business of ideas.</h1>
          <p className="lede">Every company starts in the dark. We build what comes after.</p>
        </section>

        <section className="strike" id="strike">
          <div>
            <p className="eyebrow">01 — The strike</p>
            <h2>Then someone <em>flicks it on.</em></h2>
            <p className="lede" style={{ marginLeft: "auto" }}>One idea, lit. Everything we make is a room that light reaches.</p>
          </div>
        </section>

        {PRODUCTS.map((p) => (
          <section className="prod" id={p.id} key={p.id}>
            <div>
              <div className="idx">{p.index} <span className={`tag${p.tag === "Live" ? " live" : ""}`}>{p.tag}</span></div>
              <h2>{p.name}</h2>
              <p>{p.blurb}{"href" in p && p.href ? <> <a href={p.href}>{p.href.replace("https://", "")}</a></> : null}</p>
            </div>
          </section>
        ))}

        <section className="vision" id="vision">
          <div>
            <p className="eyebrow">One bulb, many rooms</p>
            <h2>Light is the only thing that gets bigger when you share it.</h2>
            <p className="lede">The Lightbulb Company is a holding company for products that make people more capable. Each one starts as a single idea, gets built by a small team, and stays lit long after launch.</p>
          </div>
        </section>
      </main>

      <footer>
        <div>
          <div className="mark"><SunriseMark /></div>
          <a className="mail" href="mailto:hello@thelightbulb.company">hello@thelightbulb.company</a>
          <div className="meta">The business of ideas · Isol8 Inc., doing business as Lightbulb AI</div>
        </div>
        <div className="cols">
          <div>Products{PRODUCTS.map((p) => <a key={p.id} href={"href" in p && p.href ? p.href : `#${p.id}`}>{p.name}</a>)}</div>
          <div>Company<a href="mailto:hello@thelightbulb.company">Contact</a></div>
        </div>
      </footer>
    </>
  );
}
```

- [ ] **Step 4: build + look**

Run: `pnpm --filter @lightbulb/company run build` — expected exit 0.
Run (background): `pnpm --filter @lightbulb/company run dev` → http://localhost:3100.
With Claude-in-Chrome (never Playwright — owner rule): open the page, scroll by mouse wheel to each section top (`hero`, `strike`, `ai`, `goose`, `ventures`, `physical`, `vision`, footer), screenshot each, and compare against `docs/superpowers/specs/2026-08-28-company-site-prototype.html` served from the same machine. Check specifically: (a) the canvas paints on first load without a pointer move (demand loop + ScrollTrigger's initial `onUpdate`; if it does not, call `invalidate()` from the `layout()` effect — it already does — and from a 300 ms timeout), (b) the brain is visible inside the glass in the strike chapter (depth-write fix intact), (c) each shape is fully formed at its section top, (d) the rail dot matches the chapter, (e) the label fades out before the vision chapter, (f) at rest `requestAnimationFrame` stops (check with `read_console_messages` after adding a temporary `console.count("frame")` — remove it before commit).
Then `read_console_messages` with pattern `error|Error|THREE` — expected: none except the `THREE.Clock` deprecation (R3F's).

- [ ] **Step 5: Commit**

```bash
git add apps/company/src apps/company/public
git commit -m "feat(company): the page — copy, chrome, logo marks"
```

---

### Task 5: PR CI for the company app

**Files:**
- Create: `.github/workflows/company-ci.yml`

- [ ] **Step 1: the workflow (mirrors frontend-ci.yml, minus the frontend's env)**

```yaml
# =============================================================================
# Company site CI (PRs only) — apps/company, thelightbulb.company
# =============================================================================
name: Company CI

on:
  pull_request:
    branches: [main]
    paths:
      - 'apps/company/**'
      - '.github/workflows/company-ci.yml'
      - 'pnpm-lock.yaml'

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

env:
  NODE_VERSION: "20"

jobs:
  lint-test-build:
    name: Lint, test & build
    runs-on: ${{ vars.CI_RUNNER }}
    timeout-minutes: 20
    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Setup pnpm
        uses: pnpm/action-setup@v4
        with:
          dest: ${{ runner.temp }}/setup-pnpm

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: ${{ env.NODE_VERSION }}
          cache: 'pnpm'

      - name: Install dependencies
        run: pnpm install --frozen-lockfile

      - name: Lint
        run: pnpm --filter @lightbulb/company run lint

      - name: Type check
        run: pnpm --filter @lightbulb/company run typecheck

      - name: Unit tests
        run: pnpm --filter @lightbulb/company run test

      - name: Build
        run: pnpm --filter @lightbulb/company run build
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/company-ci.yml
git commit -m "ci(company): lint, test and build apps/company on PRs"
```

---

### Task 6: Deploy steps in the CDK pipeline (dev on merge, prod on dispatch)

**Files:**
- Modify: `apps/infra/lib/app.ts` (dev stage `post:` array after `DeployVercelDev`; prod stage `post:` array after `DeployVercelProd`; the `workflowFile.patch(...)` call that adds `/jobs/prod-DeployVercelProd/if`)
- Modify: `apps/infra/test/deploy-prod-promotion.test.ts`
- Regenerate: `.github/workflows/deploy.yml`

**Interfaces:**
- Consumes: a GitHub secret `VERCEL_PROJECT_ID_COMPANY` (created in Task 7 — the workflow only reads it at run time, so the order does not matter for the PR).

- [ ] **Step 1: shared env + the two steps**

Next to `vercelEnv` in `app.ts`:
```ts
// The company site is a second Vercel project; same token/org, its own project id.
const companyVercelEnv = { ...vercelEnv, VERCEL_PROJECT_ID: "${{ secrets.VERCEL_PROJECT_ID_COMPANY }}" };
const COMPANY_DOMAIN = "thelightbulb.company";
const companyVercelSteps = (prod: boolean) => [
  { name: "Checkout", uses: "actions/checkout@v4" },
  { name: "Setup pnpm", uses: "pnpm/action-setup@v4" },
  { name: "Setup Node.js", uses: "actions/setup-node@v4", with: { "node-version": "20", cache: "pnpm" } },
  { name: "Install Vercel CLI", run: "npm install -g vercel" },
  { name: "Pull Vercel Settings", run: `vercel pull --yes${prod ? " --environment=production" : ""} --token=$VERCEL_TOKEN`, env: companyVercelEnv },
  { name: "Build Company Site", run: `vercel build${prod ? " --prod" : ""} --token=$VERCEL_TOKEN`, env: companyVercelEnv },
];
```

Dev stage `post:` — append after the existing `DeployVercelDev` step:
```ts
    // The holding-company site (apps/company). Preview only in dev: the
    // domain's `dev.` subdomain is the dev agent-mail suffix (SES), so no
    // dev alias is registered on it.
    new GitHubActionStep("DeployCompanyDev", {
      jobSteps: [
        ...companyVercelSteps(false),
        { name: "Deploy to Vercel (Preview)", run: "vercel deploy --prebuilt --token=$VERCEL_TOKEN", env: companyVercelEnv },
      ],
    }),
```

Prod stage `post:` — append after the existing `DeployVercelProd` step:
```ts
    new GitHubActionStep("DeployCompanyProd", {
      jobSteps: [
        ...companyVercelSteps(true),
        {
          name: "Deploy to Vercel (Production)",
          id: "company-deploy-prod",
          run: 'DEPLOY_URL=$(vercel deploy --prebuilt --prod --token=$VERCEL_TOKEN) && echo "DEPLOY_URL=$DEPLOY_URL" >> $GITHUB_OUTPUT',
          env: companyVercelEnv,
        },
        // Register before aliasing — same SSO-wall trap as the product apex.
        { name: `Register ${COMPANY_DOMAIN}`, run: `vercel domains add ${COMPANY_DOMAIN} company --force --token="$VERCEL_TOKEN"`, env: companyVercelEnv },
        { name: `Alias to ${COMPANY_DOMAIN}`, run: `vercel alias \${{ steps.company-deploy-prod.outputs.DEPLOY_URL }} ${COMPANY_DOMAIN} --token=$VERCEL_TOKEN`, env: companyVercelEnv },
        { name: `Register www.${COMPANY_DOMAIN}`, run: `vercel domains add www.${COMPANY_DOMAIN} company --force --token="$VERCEL_TOKEN"`, env: companyVercelEnv },
        { name: `Alias to www.${COMPANY_DOMAIN}`, run: `vercel alias \${{ steps.company-deploy-prod.outputs.DEPLOY_URL }} www.${COMPANY_DOMAIN} --token=$VERCEL_TOKEN`, env: companyVercelEnv },
      ],
    }),
```

The gate patch — add one line next to the existing one:
```ts
  JsonPatch.add("/jobs/prod-DeployVercelProd/if", PROD_PROMOTE_IF),
  JsonPatch.add("/jobs/prod-DeployCompanyProd/if", PROD_PROMOTE_IF),
```

- [ ] **Step 2: extend the infra test**

In `apps/infra/test/deploy-prod-promotion.test.ts`, add to the `gates the prod frontend release specifically` block:
```ts
    // The holding-company site is a second GitHubActionStep in the prod stage,
    // gated by its own patch line — a forgotten patch ships it on merge.
    expect(jobs.has("prod-DeployCompanyProd")).toBe(true);
    expect(jobLevelIf(jobs.get("prod-DeployCompanyProd")!)).toBe(PROMOTE_IF);
```
and to `leaves dev, synth and asset jobs ungated` nothing (the `dev-` prefix already covers `dev-DeployCompanyDev`).

- [ ] **Step 3: synth, run infra tests**

Run: `cd apps/infra && npx cdk synth --no-notices > /dev/null && cd ../..` (no Docker needed — `local.tryBundle`; if synth demotes to Docker, `open -a OrbStack` first).
Run: `git diff --stat -- .github/workflows/deploy.yml` — expected: two new jobs `dev-DeployCompanyDev` and `prod-DeployCompanyProd`, the latter with `if: github.event_name == 'workflow_dispatch'` at job level.
Run: `pnpm --filter @lightbulb/infra run test -- deploy-prod-promotion` — expected PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/infra/lib/app.ts apps/infra/test/deploy-prod-promotion.test.ts .github/workflows/deploy.yml
git commit -m "infra(company): deploy thelightbulb.company from the pipeline — preview on merge, prod on dispatch"
```

---

### Task 7: Vercel project, secret, DNS, PR

These are operations, not code. Record every value in the PR description.

- [ ] **Step 1: create the Vercel project and capture its id**

Run (in `apps/company`): `vercel link --yes --project company` → creates/links the `company` project under the `prez2307` account's team; `.vercel/project.json` (gitignored) holds `projectId` + `orgId`.
Confirm `orgId` equals the `VERCEL_ORG_ID` secret's team (`vercel teams ls`).
Set the project's root directory: `curl -sX PATCH "https://api.vercel.com/v9/projects/<projectId>?teamId=<orgId>" -H "Authorization: Bearer $VERCEL_TOKEN" -H 'Content-Type: application/json' -d '{"rootDirectory":"apps/company","framework":"nextjs"}'` where `$VERCEL_TOKEN` is read from the CLI's own auth store (`~/Library/Application Support/com.vercel.cli/auth.json`, key `token`) — never pasted into chat. If that file is absent, ask the owner to set Root Directory = `apps/company` in the dashboard instead.

- [ ] **Step 2: the GitHub secret**

Run: `gh secret set VERCEL_PROJECT_ID_COMPANY --repo Isol8AI/isol8 --body "<projectId>"`.

- [ ] **Step 3: DNS in the mail zone (zone `Z09913772LJJ6H4X9JJX0`)**

First look: `aws route53 list-resource-record-sets --hosted-zone-id Z09913772LJJ6H4X9JJX0 --profile isol8-admin --query "ResourceRecordSets[?Name=='thelightbulb.company.' || Name=='www.thelightbulb.company.']"` — expected: MX/TXT/NS/SOA at the apex, nothing at `www`. **Stop and ask if an A/AAAA/CNAME already exists at either name.**
Then UPSERT (A at the apex coexists with MX; CNAME at `www` is a new name):
```json
{"Changes":[
 {"Action":"UPSERT","ResourceRecordSet":{"Name":"thelightbulb.company.","Type":"A","TTL":300,"ResourceRecords":[{"Value":"76.76.21.21"}]}},
 {"Action":"UPSERT","ResourceRecordSet":{"Name":"www.thelightbulb.company.","Type":"CNAME","TTL":300,"ResourceRecords":[{"Value":"cname.vercel-dns.com."}]}}
]}
```
`aws route53 change-resource-record-sets --hosted-zone-id Z09913772LJJ6H4X9JJX0 --change-batch file://…json --profile isol8-admin`.
Run `vercel domains add thelightbulb.company company` once by hand: if Vercel asks for a `_vercel` TXT verification record, add it the same way and re-run. (The prod job's `--force` add is then idempotent.)

- [ ] **Step 4: rebase, push, PR**

Run: `git fetch origin main && git rebase origin/main` (worktree branch), fix any conflicts in `deploy.yml` by re-running `cdk synth`.
Check for stray symlinks: `git ls-tree -r HEAD | awk '$1=="120000"{print $4}'` — expected: empty.
Push: `git push -u origin feat/company-site`.
PR title: `feat(company): thelightbulb.company — the holding-company site`. Body: what it is (one paragraph), the seven chapter screenshots from Task 4, the Vercel project id, the two DNS records added, the new secret name, and the note that prod ships on the next `workflow_dispatch` promotion. End with the generated-with footer.
Watch the PR checks by name: `Company CI`, `Infra CI` (drift check), CodeRabbit. Do not merge without the owner.

- [ ] **Step 5: after merge** (owner-triggered): the dev preview URL appears in the `dev-DeployCompanyDev` job log; validate it in Chrome; then the owner promotes with `gh workflow run deploy.yml --ref main` and the prod job registers + aliases the domain. Delete `docs/superpowers/specs/2026-08-28-company-site-*.md/html` and this plan in a follow-up once `https://thelightbulb.company` answers.
