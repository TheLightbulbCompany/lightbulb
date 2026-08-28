# Hero Lamp in three.js — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The landing hero starts as a dark room with an unlit pendant lamp; typing the first character of an idea (or clicking the lamp) strikes the filament and a real three.js light illuminates the room.

**Architecture:** A tiny external store (`lampStore.ts`) owns the theme and writes the existing `data-lb-theme` contract; both the three.js scene and the SVG fallback read it, and `HeroFunnel` writes to it on the first keystroke. The scene is a client-only `<Canvas>` rendered on demand: a lathe-built ST64 glass with a three-blade emissive filament, a shadow-casting `SpotLight` + shadowless `PointLight` inside it, a wall whose emissive lerps midnight → cream on strike, selective bloom on the filament only, and a code-built `Environment` so the glass reads as glass. A pure pendulum integrator drives both the 3D group and the SVG fallback.

**Tech Stack:** Next.js 16 App Router, React 19.2, `three@0.184` (already pinned), `@react-three/fiber@9.7.0`, `@react-three/drei@10.7.8`, `@react-three/postprocessing@3.1.1` + `postprocessing@6.39.4`, vitest + Testing Library (jsdom), Playwright MCP for render checks.

**Spec:** `docs/superpowers/specs/2026-08-28-hero-lamp-three-design.md` — read it first; this plan argues from it.

**Worktree / branch:** `.worktrees/brand-visual` on branch `feat/hero-lamp-three` (off `feat/lightbulb-rebrand`, PR #2090). All paths below are relative to `apps/frontend/` unless they start with `docs/`.

## Global Constraints

- Do NOT bump `three` past `0.184` — `postprocessing` pins `< 0.186`.
- Install exact versions: `@react-three/fiber@9.7.0 @react-three/drei@10.7.8 @react-three/postprocessing@3.1.1 postprocessing@6.39.4`.
- The theme contract is unchanged: `data-lb-theme="dark|light"` on `<html>`, persisted under localStorage key `lightbulb-theme`. Only `.ld-page` reads it. Initial state is `dark` unless a stored `light` exists.
- Yellow `#F0C34E` (`--bulb`) is light only — it never colours text. The Start button is the only shape it fills, and only when lit.
- One lamp: the nav mark stays; nothing else bulb-shaped is added.
- Scene colours are read from CSS variables `--midnight --cream --bulb --brass` at mount, never hardcoded.
- Brand rules: no ray fan, no platform, no decomposition of the mark (§01/§02/§07).
- Reduced motion (`prefers-reduced-motion: reduce`), SSR, and no-WebGL all get the SVG fallback (`HangingLamp`), never a broken canvas.
- Copy: the product is "Lightbulb" — never "Lightbulb AI".
- Commit after every task; run `pnpm exec eslint <files>` and `pnpm exec tsc --noEmit` before each commit.

---

### Task 1: Dependencies and the lamp store

**Files:**
- Modify: `package.json` (via `pnpm add`)
- Create: `src/components/landing/HeroLamp/lampStore.ts`
- Test: `src/components/landing/HeroLamp/__tests__/lampStore.test.ts`

**Interfaces:**
- Produces:
  ```ts
  export type LampTheme = "dark" | "light";
  export const LAMP_STORAGE_KEY = "lightbulb-theme";
  export function getTheme(): LampTheme;            // current snapshot
  export function getServerTheme(): LampTheme;      // always "dark"
  export function subscribe(cb: () => void): () => void;
  export function setTheme(t: LampTheme): void;     // writes <html data-lb-theme> + storage
  export function strike(): void;                   // toggle
  export function lightOn(): void;                  // idempotent → "light"
  export function hydrateFromStorage(): void;       // call once on client mount
  export function useLampTheme(): LampTheme;        // useSyncExternalStore hook
  ```

- [ ] **Step 1: Install the stack (exact versions)**

Run from `apps/frontend/`:
```bash
pnpm add @react-three/fiber@9.7.0 @react-three/drei@10.7.8 @react-three/postprocessing@3.1.1 postprocessing@6.39.4
```
Expected: lockfile updated; `three` stays at `^0.184.0`. Verify: `grep -E '"(three|@react-three/[a-z]+|postprocessing)"' package.json`.

- [ ] **Step 2: Write the failing store test**

`src/components/landing/HeroLamp/__tests__/lampStore.test.ts`:
```ts
import { beforeEach, describe, expect, it } from "vitest";
import {
  LAMP_STORAGE_KEY,
  getServerTheme,
  getTheme,
  hydrateFromStorage,
  lightOn,
  setTheme,
  strike,
  subscribe,
} from "../lampStore";

describe("lampStore", () => {
  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.removeAttribute("data-lb-theme");
    setTheme("dark");
  });

  it("defaults to dark on the server and the client", () => {
    expect(getServerTheme()).toBe("dark");
    expect(getTheme()).toBe("dark");
  });

  it("strike toggles and writes the contract", () => {
    strike();
    expect(getTheme()).toBe("light");
    expect(document.documentElement.getAttribute("data-lb-theme")).toBe("light");
    expect(window.localStorage.getItem(LAMP_STORAGE_KEY)).toBe("light");
    strike();
    expect(getTheme()).toBe("dark");
    expect(document.documentElement.getAttribute("data-lb-theme")).toBe("dark");
  });

  it("lightOn is idempotent and never darkens", () => {
    lightOn();
    lightOn();
    expect(getTheme()).toBe("light");
  });

  it("hydrateFromStorage adopts a stored light, ignores junk", () => {
    window.localStorage.setItem(LAMP_STORAGE_KEY, "light");
    hydrateFromStorage();
    expect(getTheme()).toBe("light");
    window.localStorage.setItem(LAMP_STORAGE_KEY, "banana");
    setTheme("dark");
    hydrateFromStorage();
    expect(getTheme()).toBe("dark");
  });

  it("notifies subscribers once per change", () => {
    let n = 0;
    const off = subscribe(() => { n += 1; });
    strike();
    expect(n).toBe(1);
    off();
    strike();
    expect(n).toBe(1);
  });
});
```

- [ ] **Step 3: Run it to verify it fails**

Run: `pnpm vitest run src/components/landing/HeroLamp/__tests__/lampStore.test.ts`
Expected: FAIL — `Cannot find module '../lampStore'`.

- [ ] **Step 4: Implement the store**

`src/components/landing/HeroLamp/lampStore.ts`:
```ts
/**
 * The lamp's state, shared by the three.js scene, the SVG fallback and the
 * hero's input. A module-level store instead of context because the writer
 * (HeroFunnel) and the reader (the lamp) are siblings in page.tsx, and a
 * provider around the whole page would be scaffolding for one boolean.
 *
 * Contract (unchanged from the SVG lamp): `data-lb-theme` on <html>,
 * persisted under `lightbulb-theme`. Only `.ld-page` reads the attribute, so
 * this cannot repaint /chat. Server snapshot is always "dark": the room
 * starts unlit, and hydration must not depend on storage.
 */
import { useSyncExternalStore } from "react";

export type LampTheme = "dark" | "light";
export const LAMP_STORAGE_KEY = "lightbulb-theme";

let theme: LampTheme = "dark";
const listeners = new Set<() => void>();

function write(t: LampTheme) {
  if (typeof document !== "undefined") {
    document.documentElement.setAttribute("data-lb-theme", t);
  }
  try {
    window.localStorage.setItem(LAMP_STORAGE_KEY, t);
  } catch {
    // private mode — the toggle still works for this page view
  }
}

export function getTheme(): LampTheme {
  return theme;
}

export function getServerTheme(): LampTheme {
  return "dark";
}

export function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

export function setTheme(t: LampTheme): void {
  if (t === theme) {
    write(t); // keep the DOM/storage in step even on a no-op set
    return;
  }
  theme = t;
  write(t);
  listeners.forEach((cb) => cb());
}

export function strike(): void {
  setTheme(theme === "dark" ? "light" : "dark");
}

export function lightOn(): void {
  if (theme !== "light") setTheme("light");
}

export function hydrateFromStorage(): void {
  try {
    const v = window.localStorage.getItem(LAMP_STORAGE_KEY);
    if (v === "light" || v === "dark") setTheme(v);
    else write(theme);
  } catch {
    write(theme);
  }
}

export function useLampTheme(): LampTheme {
  return useSyncExternalStore(subscribe, getTheme, getServerTheme);
}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pnpm vitest run src/components/landing/HeroLamp/__tests__/lampStore.test.ts`
Expected: PASS (5 tests).

- [ ] **Step 6: Lint, typecheck, commit**

```bash
pnpm exec eslint src/components/landing/HeroLamp
pnpm exec tsc --noEmit
git add package.json ../../pnpm-lock.yaml src/components/landing/HeroLamp
git commit -m "feat(landing): lamp store — one theme, three readers, the data-lb-theme contract kept"
```

---

### Task 2: The pendulum integrator

**Files:**
- Create: `src/components/landing/HeroLamp/pendulum.ts`
- Test: `src/components/landing/HeroLamp/__tests__/pendulum.test.ts`

**Interfaces:**
- Produces:
  ```ts
  export interface Pendulum {
    angle: number;                 // radians
    step(): void;                  // one frame (~16ms) of damped motion
    impulse(v: number): void;      // add angular velocity, clamped
    settled(): boolean;            // true when angle & velocity ≈ 0
    reset(): void;
  }
  export function createPendulum(opts?: { stiffness?: number; damping?: number }): Pendulum;
  export const SCROLL_IMPULSE = 0.00016;   // per px of scroll delta
  export const IMPULSE_CLAMP = 0.006;
  ```

- [ ] **Step 1: Write the failing test**

`src/components/landing/HeroLamp/__tests__/pendulum.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import { IMPULSE_CLAMP, createPendulum } from "../pendulum";

describe("pendulum", () => {
  it("starts settled at rest", () => {
    const p = createPendulum();
    expect(p.angle).toBe(0);
    expect(p.settled()).toBe(true);
  });

  it("an impulse produces motion that decays back to rest", () => {
    const p = createPendulum();
    p.impulse(0.004);
    p.step();
    expect(p.settled()).toBe(false);
    let maxAngle = 0;
    for (let i = 0; i < 2000; i += 1) {
      p.step();
      maxAngle = Math.max(maxAngle, Math.abs(p.angle));
    }
    expect(maxAngle).toBeGreaterThan(0.01);
    expect(p.settled()).toBe(true);
    expect(p.angle).toBe(0);
  });

  it("clamps impulses so a fast scroll cannot fling the lamp", () => {
    const p = createPendulum();
    p.impulse(10);
    p.step();
    // one clamped kick must stay well under a quarter turn
    let maxAngle = 0;
    for (let i = 0; i < 400; i += 1) {
      p.step();
      maxAngle = Math.max(maxAngle, Math.abs(p.angle));
    }
    expect(maxAngle).toBeLessThan(0.4);
    expect(IMPULSE_CLAMP).toBe(0.006);
  });

  it("reset returns to rest immediately", () => {
    const p = createPendulum();
    p.impulse(0.005);
    p.step();
    p.reset();
    expect(p.angle).toBe(0);
    expect(p.settled()).toBe(true);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm vitest run src/components/landing/HeroLamp/__tests__/pendulum.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

`src/components/landing/HeroLamp/pendulum.ts` — the integrator lifted verbatim from `HangingLamp.tsx` (stiffness 0.014, damping 0.986, settle threshold 0.0004), made pure so the 3D group and the SVG share it:
```ts
/**
 * Damped pendulum, frame-stepped. Lifted from the SVG lamp so the three.js
 * group and the fallback swing identically. Constants are the tuned values
 * from that component — do not "correct" them without re-tuning by eye.
 */
export interface Pendulum {
  angle: number;
  step(): void;
  impulse(v: number): void;
  settled(): boolean;
  reset(): void;
}

export const SCROLL_IMPULSE = 0.00016;
export const IMPULSE_CLAMP = 0.006;
const SETTLE = 0.0004;

export function createPendulum(opts: { stiffness?: number; damping?: number } = {}): Pendulum {
  const stiffness = opts.stiffness ?? 0.014;
  const damping = opts.damping ?? 0.986;
  let velocity = 0;
  const p: Pendulum = {
    angle: 0,
    step() {
      velocity += -stiffness * Math.sin(p.angle);
      velocity *= damping;
      p.angle += velocity;
      if (Math.abs(p.angle) < SETTLE && Math.abs(velocity) < SETTLE) {
        p.angle = 0;
        velocity = 0;
      }
    },
    impulse(v: number) {
      velocity += Math.max(-IMPULSE_CLAMP, Math.min(IMPULSE_CLAMP, v));
    },
    settled() {
      return p.angle === 0 && velocity === 0;
    },
    reset() {
      p.angle = 0;
      velocity = 0;
    },
  };
  return p;
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pnpm vitest run src/components/landing/HeroLamp/__tests__/pendulum.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
pnpm exec eslint src/components/landing/HeroLamp && pnpm exec tsc --noEmit
git add src/components/landing/HeroLamp
git commit -m "feat(landing): pendulum integrator, pure — one swing for the 3D lamp and the SVG fallback"
```

---

### Task 3: Refactor the SVG lamp onto the store and integrator (it becomes the fallback)

**Files:**
- Modify: `src/components/landing/HangingLamp.tsx` (whole file — replace `useState`/storage/pendulum with the store + integrator; keep the SVG markup exactly)
- Test: `src/components/landing/__tests__/HangingLamp.test.tsx`

**Interfaces:**
- Consumes: `useLampTheme`, `strike`, `hydrateFromStorage` (Task 1); `createPendulum`, `SCROLL_IMPULSE` (Task 2).
- Produces: `export function HangingLamp()` — unchanged signature; now dark by default.

- [ ] **Step 1: Write the failing test**

`src/components/landing/__tests__/HangingLamp.test.tsx`:
```tsx
import { beforeEach, describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { HangingLamp } from "../HangingLamp";
import { getTheme, setTheme } from "../HeroLamp/lampStore";

describe("HangingLamp (SVG fallback)", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setTheme("dark");
  });

  it("starts dark and offers to turn the light on", () => {
    render(<HangingLamp />);
    expect(screen.getByRole("button", { name: "Turn on the light" })).toBeInTheDocument();
    expect(document.documentElement.getAttribute("data-lb-theme")).toBe("dark");
  });

  it("clicking the glass strikes the lamp and relabels the control", () => {
    render(<HangingLamp />);
    fireEvent.click(screen.getByRole("button", { name: "Turn on the light" }));
    expect(getTheme()).toBe("light");
    expect(screen.getByRole("button", { name: "Turn off the light" })).toBeInTheDocument();
  });

  it("Enter and Space on the glass also strike", () => {
    render(<HangingLamp />);
    const glass = screen.getByRole("button", { name: "Turn on the light" });
    fireEvent.keyDown(glass, { key: "Enter" });
    expect(getTheme()).toBe("light");
    fireEvent.keyDown(screen.getByRole("button", { name: "Turn off the light" }), { key: " " });
    expect(getTheme()).toBe("dark");
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm vitest run src/components/landing/__tests__/HangingLamp.test.tsx`
Expected: FAIL — the first test fails because the current component starts `light` ("Turn off the light" is what renders).

- [ ] **Step 3: Rewrite the component's state plumbing**

Replace `src/components/landing/HangingLamp.tsx` lines 19–98 (imports through `strike`) with the following; keep the JSX from `return (` to the end exactly as it is:
```tsx
import * as React from "react";
import { hydrateFromStorage, strike, useLampTheme } from "./HeroLamp/lampStore";
import { SCROLL_IMPULSE, createPendulum } from "./HeroLamp/pendulum";

const GLASS = "M 86,150 L 72,132 L 72,76 A 28,32 0 0 1 128,76 L 128,132 L 114,150 Z";

export function HangingLamp() {
  const hostRef = React.useRef<HTMLDivElement>(null);
  const glassRef = React.useRef<SVGPathElement>(null);
  const theme = useLampTheme();

  // Adopt a stored choice after hydration. The server renders dark — the
  // room starts unlit — so storage is never read during render.
  React.useEffect(() => {
    hydrateFromStorage();
    return () => document.documentElement.removeAttribute("data-lb-theme");
  }, []);

  // Pendulum. transform only — never a layout property.
  React.useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    const pendulum = createPendulum();
    let raf = 0;
    let lastY = window.scrollY;

    const frame = () => {
      pendulum.step();
      host.style.transform = `rotate(${pendulum.angle}rad)`;
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);

    const onScroll = () => {
      const delta = window.scrollY - lastY;
      lastY = window.scrollY;
      pendulum.impulse(delta * SCROLL_IMPULSE);
    };
    window.addEventListener("scroll", onScroll, { passive: true });

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("scroll", onScroll);
    };
  }, []);
```
Also update the header comment's last paragraph to: `Initial state is dark — the room starts unlit; typing an idea or clicking the glass lights it.` Delete the now-unused `STORAGE_KEY`, `Theme`, `readStored`, and the local `strike` callback (the JSX's `onClick={strike}` now refers to the imported one).

- [ ] **Step 4: Run the test to verify it passes**

Run: `pnpm vitest run src/components/landing/__tests__/HangingLamp.test.tsx src/components/landing`
Expected: PASS, and every pre-existing landing test still passes.

- [ ] **Step 5: Commit**

```bash
pnpm exec eslint src/components/landing/HangingLamp.tsx && pnpm exec tsc --noEmit
git add src/components/landing/HangingLamp.tsx src/components/landing/__tests__/HangingLamp.test.tsx
git commit -m "refactor(landing): the SVG lamp reads the shared store and starts dark"
```

---

### Task 4: First keystroke lights the room

**Files:**
- Modify: `src/components/landing/HeroFunnel.tsx:86-89` (the input's `onChange`)
- Test: `src/components/landing/__tests__/HeroFunnel.test.tsx`

**Interfaces:**
- Consumes: `lightOn`, `getTheme` (Task 1).

- [ ] **Step 1: Write the failing test**

`src/components/landing/__tests__/HeroFunnel.test.tsx`:
```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { HeroFunnel } from "../HeroFunnel";
import { getTheme, setTheme } from "../HeroLamp/lampStore";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

describe("HeroFunnel lights the room", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    setTheme("dark");
  });

  it("the first character typed turns the light on", () => {
    render(<HeroFunnel />);
    const input = screen.getByRole("textbox", { name: /your idea/i });
    expect(getTheme()).toBe("dark");
    fireEvent.change(input, { target: { value: "A" } });
    expect(getTheme()).toBe("light");
  });

  it("typing never turns the light back off", () => {
    render(<HeroFunnel />);
    const input = screen.getByRole("textbox", { name: /your idea/i });
    fireEvent.change(input, { target: { value: "A" } });
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.change(input, { target: { value: "B" } });
    expect(getTheme()).toBe("light");
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm vitest run src/components/landing/__tests__/HeroFunnel.test.tsx`
Expected: FAIL — `getTheme()` is still `"dark"` after typing.

- [ ] **Step 3: Wire the keystroke**

In `src/components/landing/HeroFunnel.tsx` add the import:
```ts
import { lightOn } from "./HeroLamp/lampStore";
```
and change the input's `onChange` (currently lines 86–89) to:
```tsx
            onChange={(e) => {
              const next = e.target.value;
              // "Type an idea. Watch it become a company." — the first
              // character IS the lightbulb moment. Only ever lights, never
              // darkens; the lamp itself is the toggle.
              if (next.length > 0) lightOn();
              setInput(next);
              stash(path, next);
            }}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pnpm vitest run src/components/landing/__tests__/HeroFunnel.test.tsx src/components/landing`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
pnpm exec eslint src/components/landing/HeroFunnel.tsx && pnpm exec tsc --noEmit
git add src/components/landing/HeroFunnel.tsx src/components/landing/__tests__/HeroFunnel.test.tsx
git commit -m "feat(landing): typing the first character of an idea turns on the light"
```

---

### Task 5: Lamp geometry helpers and the glow texture

**Files:**
- Create: `src/components/landing/HeroLamp/lampGeometry.ts`
- Create: `src/components/landing/HeroLamp/glowTexture.ts`
- Test: `src/components/landing/HeroLamp/__tests__/lampGeometry.test.ts`

**Interfaces:**
- Produces:
  ```ts
  // lampGeometry.ts
  export const ST64_PROFILE: ReadonlyArray<readonly [number, number]>; // [radius, height] bottom→apex, height 0..1.10
  export const GLASS_HEIGHT = 1.1;
  export const GLASS_MAX_RADIUS = 0.34;
  export interface BladeSpec { tiltDeg: number; }         // rotation about Z, degrees
  export const BLADES: readonly BladeSpec[];               // three blades: -12, 0, +12
  export const TIE_HEIGHTS: readonly [number, number];     // fractions of blade length: 0.38, 0.72
  export const COLLAR_RINGS: ReadonlyArray<readonly [radius: number, height: number]>; // three stepped rings
  export function cssColor(name: "--midnight" | "--cream" | "--bulb" | "--brass", fallback: string): string;
  // glowTexture.ts
  export function makeGlowTexture(size?: number): import("three").CanvasTexture; // radial white→transparent
  ```

- [ ] **Step 1: Write the failing test**

`src/components/landing/HeroLamp/__tests__/lampGeometry.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import {
  BLADES,
  COLLAR_RINGS,
  GLASS_HEIGHT,
  GLASS_MAX_RADIUS,
  ST64_PROFILE,
  TIE_HEIGHTS,
  cssColor,
} from "../lampGeometry";

describe("ST64 profile (§02: straight sides rising to one elliptical dome)", () => {
  it("runs from the neck at the bottom to a closed apex at the top", () => {
    const first = ST64_PROFILE[0];
    const last = ST64_PROFILE[ST64_PROFILE.length - 1];
    expect(first[1]).toBe(0);
    expect(last[0]).toBe(0);
    expect(last[1]).toBe(GLASS_HEIGHT);
  });

  it("height is strictly increasing so the lathe cannot fold on itself", () => {
    for (let i = 1; i < ST64_PROFILE.length; i += 1) {
      expect(ST64_PROFILE[i][1]).toBeGreaterThan(ST64_PROFILE[i - 1][1]);
    }
  });

  it("widest point is the shoulder, not the neck and not the dome", () => {
    const widest = ST64_PROFILE.reduce((a, b) => (b[0] > a[0] ? b : a));
    expect(widest[0]).toBe(GLASS_MAX_RADIUS);
    expect(widest[1]).toBeGreaterThan(GLASS_HEIGHT * 0.5);
    expect(widest[1]).toBeLessThan(GLASS_HEIGHT * 0.85);
  });
});

describe("filament (§02: three blades, tied twice)", () => {
  it("is exactly three blades, symmetric about the centre one", () => {
    expect(BLADES).toHaveLength(3);
    expect(BLADES.map((b) => b.tiltDeg)).toEqual([-12, 0, 12]);
  });
  it("has exactly two ties, both inside the blade span", () => {
    expect(TIE_HEIGHTS).toHaveLength(2);
    for (const h of TIE_HEIGHTS) {
      expect(h).toBeGreaterThan(0);
      expect(h).toBeLessThan(1);
    }
  });
});

describe("collar", () => {
  it("is three stepped rings that narrow downward", () => {
    expect(COLLAR_RINGS).toHaveLength(3);
    expect(COLLAR_RINGS[0][0]).toBeGreaterThan(COLLAR_RINGS[1][0]);
    expect(COLLAR_RINGS[1][0]).toBeGreaterThan(COLLAR_RINGS[2][0]);
  });
});

describe("cssColor", () => {
  it("returns the fallback when the variable is unset", () => {
    expect(cssColor("--bulb", "#F0C34E")).toBe("#F0C34E");
  });
  it("reads a set variable", () => {
    document.documentElement.style.setProperty("--bulb", "#123456");
    expect(cssColor("--bulb", "#F0C34E")).toBe("#123456");
    document.documentElement.style.removeProperty("--bulb");
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm vitest run src/components/landing/HeroLamp/__tests__/lampGeometry.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the geometry constants**

`src/components/landing/HeroLamp/lampGeometry.ts`:
```ts
/**
 * The lamp's anatomy as numbers, per Brand Identity Manual §02 so drift is
 * detectable: ST64 glass (straight sides rising to a single elliptical
 * dome), three tapered blades tied twice, a stepped collar. Scene units:
 * the glass is 1.1 tall.
 */
export const GLASS_HEIGHT = 1.1;
export const GLASS_MAX_RADIUS = 0.34;

/** [radius, height], neck → apex. Fed to THREE.LatheGeometry as Vector2s. */
export const ST64_PROFILE: ReadonlyArray<readonly [number, number]> = [
  [0.14, 0.0],
  [0.15, 0.08],
  [0.2, 0.22],
  [0.27, 0.42],  // straight sides…
  [0.32, 0.62],
  [0.34, 0.78],  // …to the shoulder
  [0.3, 0.92],   // one elliptical dome
  [0.2, 1.02],
  [0.09, 1.08],
  [0.0, 1.1],
];

export interface BladeSpec {
  tiltDeg: number;
}
/** Three, not four — and the two ties are what stop it reading as a sunburst. */
export const BLADES: readonly BladeSpec[] = [{ tiltDeg: -12 }, { tiltDeg: 0 }, { tiltDeg: 12 }];
export const TIE_HEIGHTS: readonly [number, number] = [0.38, 0.72];

/** [radius, height] top → bottom. */
export const COLLAR_RINGS: ReadonlyArray<readonly [number, number]> = [
  [0.16, 0.05],
  [0.15, 0.05],
  [0.13, 0.04],
];

export function cssColor(
  name: "--midnight" | "--cream" | "--bulb" | "--brass",
  fallback: string,
): string {
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}
```

- [ ] **Step 4: Implement the glow texture**

`src/components/landing/HeroLamp/glowTexture.ts`:
```ts
import { CanvasTexture, SRGBColorSpace } from "three";

/**
 * A soft radial disc for the filament's halo — drawn in code so there is no
 * asset to fetch and nothing to fall out of step with the palette (the
 * sprite's material tints it with --bulb).
 */
export function makeGlowTexture(size = 128): CanvasTexture {
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d")!;
  const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  g.addColorStop(0, "rgba(255,255,255,1)");
  g.addColorStop(0.25, "rgba(255,255,255,0.55)");
  g.addColorStop(0.6, "rgba(255,255,255,0.12)");
  g.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  const tex = new CanvasTexture(canvas);
  tex.colorSpace = SRGBColorSpace;
  return tex;
}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pnpm vitest run src/components/landing/HeroLamp/__tests__/lampGeometry.test.ts`
Expected: PASS (8 tests).

- [ ] **Step 6: Commit**

```bash
pnpm exec eslint src/components/landing/HeroLamp && pnpm exec tsc --noEmit
git add src/components/landing/HeroLamp
git commit -m "feat(landing): the lamp's anatomy as numbers (§02) and a code-drawn glow"
```

---

### Task 6: `Lamp.tsx` — meshes, lights, pendulum group

**Files:**
- Create: `src/components/landing/HeroLamp/Lamp.tsx`

**Interfaces:**
- Consumes: Task 2 `Pendulum`; Task 5 geometry + `makeGlowTexture`.
- Produces:
  ```tsx
  export interface LampColors { bulb: string; brass: string; midnight: string; cream: string }
  export interface LampProps {
    lit: number;            // 0..1 — driven by the scene's strike tween each frame
    pendulum: Pendulum;     // stepped by the scene; Lamp only reads .angle
    colors: LampColors;
    shadows: boolean;       // quality ladder
    shadowMapSize: number;  // 512 | 1024
  }
  export function Lamp(props: LampProps): JSX.Element;
  export const LAMP_ANCHOR_Y = 3.2;    // cord pivot, world units (above the visible top)
  export const GLASS_CENTER_Y = 1.7;   // where the filament/lights sit, world units
  ```
  `Lamp` is a plain R3F component; it reads `lit` per frame through a ref so a tween from the parent never re-renders React.

No unit test — WebGL. Typecheck + the Task 9 render verify it.

- [ ] **Step 1: Write the component**

`src/components/landing/HeroLamp/Lamp.tsx`:
```tsx
"use client";

import * as React from "react";
import * as THREE from "three";
import { useFrame } from "@react-three/fiber";
import type { Pendulum } from "./pendulum";
import {
  BLADES,
  COLLAR_RINGS,
  GLASS_HEIGHT,
  ST64_PROFILE,
  TIE_HEIGHTS,
} from "./lampGeometry";
import { makeGlowTexture } from "./glowTexture";

export interface LampColors {
  bulb: string;
  brass: string;
  midnight: string;
  cream: string;
}
export interface LampProps {
  lit: number;
  pendulum: Pendulum;
  colors: LampColors;
  shadows: boolean;
  shadowMapSize: number;
}

export const LAMP_ANCHOR_Y = 3.2;
export const GLASS_CENTER_Y = 1.7;

// Distances from the pivot down the cord. Glass neck sits at the collar's
// bottom; the filament hangs inside the glass around its centre.
const CORD_LEN = LAMP_ANCHOR_Y - (GLASS_CENTER_Y + GLASS_HEIGHT * 0.55) - 0.14;
const COLLAR_TOP_Y = LAMP_ANCHOR_Y - CORD_LEN;
const COLLAR_H = COLLAR_RINGS.reduce((s, r) => s + r[1], 0);
const GLASS_NECK_Y = COLLAR_TOP_Y - COLLAR_H;      // lathe y=0 sits here
const FIL_LEN = 0.55;
const FIL_BASE_Y = GLASS_NECK_Y + 0.16;             // stem foot inside the neck

// Lit-state targets. Tuned by render (Task 9); these are the starting values.
const SPOT_INTENSITY = 28;
const POINT_INTENSITY = 9;
const EMISSIVE_ON = 6;
const EMISSIVE_OFF = 0.12;

const easeStrike = (t: number) => 1 - Math.pow(1 - t, 3); // fast attack, slow settle

export function Lamp({ lit, pendulum, colors, shadows, shadowMapSize }: LampProps) {
  const group = React.useRef<THREE.Group>(null);
  const spot = React.useRef<THREE.SpotLight>(null);
  const point = React.useRef<THREE.PointLight>(null);
  const spotTarget = React.useRef<THREE.Object3D>(new THREE.Object3D());
  const filMat = React.useRef<THREE.MeshStandardMaterial>(null);
  const glowMat = React.useRef<THREE.SpriteMaterial>(null);
  const litRef = React.useRef(lit);
  litRef.current = lit;

  const glassGeom = React.useMemo(
    () => new THREE.LatheGeometry(ST64_PROFILE.map(([r, y]) => new THREE.Vector2(r, y)), 56),
    [],
  );
  const glowTex = React.useMemo(() => makeGlowTexture(), []);
  React.useEffect(() => () => { glassGeom.dispose(); glowTex.dispose(); }, [glassGeom, glowTex]);

  const bulb = React.useMemo(() => new THREE.Color(colors.bulb), [colors.bulb]);
  const brass = React.useMemo(() => new THREE.Color(colors.brass), [colors.brass]);
  const midnight = React.useMemo(() => new THREE.Color(colors.midnight), [colors.midnight]);
  const cream = React.useMemo(() => new THREE.Color(colors.cream), [colors.cream]);

  // The spot's target must live in the scene graph and swing with the lamp.
  React.useEffect(() => {
    const g = group.current;
    const s = spot.current;
    if (!g || !s) return;
    const t = spotTarget.current;
    t.position.set(0, -(LAMP_ANCHOR_Y + 2.5), -1.6); // down the wall, in group space
    g.add(t);
    s.target = t;
    return () => { g.remove(t); };
  }, []);

  useFrame(() => {
    const g = group.current;
    if (!g) return;
    g.rotation.z = pendulum.angle;
    const k = easeStrike(litRef.current);
    if (spot.current) spot.current.intensity = SPOT_INTENSITY * k;
    if (point.current) point.current.intensity = POINT_INTENSITY * k;
    if (filMat.current) {
      filMat.current.emissive.copy(brass).lerp(bulb, k);
      filMat.current.emissiveIntensity = EMISSIVE_OFF + (EMISSIVE_ON - EMISSIVE_OFF) * k;
    }
    if (glowMat.current) glowMat.current.opacity = k;
  });

  const filY = FIL_BASE_Y + FIL_LEN / 2;

  return (
    <group ref={group} position={[0, LAMP_ANCHOR_Y, 0]}>
      {/* cord — from the pivot down to the collar */}
      <mesh position={[0, -CORD_LEN / 2, 0]} castShadow={shadows}>
        <cylinderGeometry args={[0.012, 0.012, CORD_LEN, 8]} />
        <meshStandardMaterial color={midnight} roughness={0.9} />
      </mesh>

      {/* collar — three stepped rings */}
      {COLLAR_RINGS.reduce<{ y: number; nodes: React.ReactNode[] }>(
        (acc, [r, h], i) => {
          const y = acc.y - h / 2;
          acc.nodes.push(
            <mesh key={i} position={[0, y - LAMP_ANCHOR_Y, 0]} castShadow={shadows}>
              <cylinderGeometry args={[r, r, h, 32]} />
              <meshStandardMaterial color={midnight} metalness={0.6} roughness={0.35} />
            </mesh>,
          );
          acc.y -= h;
          return acc;
        },
        { y: COLLAR_TOP_Y, nodes: [] },
      ).nodes}

      {/* glass — ST64 lathe; neck at GLASS_NECK_Y, apex above the collar? No:
          the bulb hangs BELOW the collar, so the lathe is flipped. */}
      <mesh
        geometry={glassGeom}
        position={[0, GLASS_NECK_Y - LAMP_ANCHOR_Y, 0]}
        rotation={[Math.PI, 0, 0]}
        castShadow={shadows}
      >
        <meshPhysicalMaterial
          color={cream}
          transmission={1}
          thickness={0.5}
          roughness={0.08}
          ior={1.5}
          envMapIntensity={1}
          side={THREE.DoubleSide}
        />
      </mesh>

      {/* filament — three tapered blades converging at the stem, tied twice */}
      <group position={[0, filY - LAMP_ANCHOR_Y - GLASS_HEIGHT * 0.5 - 0.1, 0]}>
        {BLADES.map((b) => (
          <mesh
            key={b.tiltDeg}
            rotation={[0, 0, (b.tiltDeg * Math.PI) / 180]}
            position={[Math.sin((b.tiltDeg * Math.PI) / 180) * 0.11, 0, 0]}
          >
            {/* narrow at the top, wide at the base: a tapered blade */}
            <cylinderGeometry args={[0.006, 0.026, FIL_LEN, 6]} />
            <meshStandardMaterial
              ref={filMat}
              color={brass}
              emissive={brass}
              emissiveIntensity={EMISSIVE_OFF}
              toneMapped={false}
            />
          </mesh>
        ))}
        {TIE_HEIGHTS.map((f) => (
          <mesh key={f} position={[0, -FIL_LEN / 2 + FIL_LEN * f, 0]} rotation={[Math.PI / 2, 0, 0]}>
            <torusGeometry args={[0.1, 0.005, 6, 28]} />
            <meshStandardMaterial color={brass} emissive={brass} emissiveIntensity={0.4} toneMapped={false} />
          </mesh>
        ))}
        {/* halo — additive, tinted by --bulb, opacity driven by lit */}
        <sprite scale={[1.7, 1.7, 1]}>
          <spriteMaterial
            ref={glowMat}
            map={glowTex}
            color={bulb}
            transparent
            opacity={0}
            depthWrite={false}
            blending={THREE.AdditiveBlending}
            toneMapped={false}
          />
        </sprite>
        {/* the lights live where the filament is */}
        <spotLight
          ref={spot}
          color={bulb}
          intensity={0}
          angle={0.95}
          penumbra={0.7}
          decay={2}
          distance={0}
          castShadow={shadows}
          shadow-mapSize-width={shadowMapSize}
          shadow-mapSize-height={shadowMapSize}
          shadow-bias={-0.0004}
          shadow-camera-near={0.3}
          shadow-camera-far={12}
        />
        <pointLight ref={point} color={bulb} intensity={0} decay={2} distance={7} />
      </group>
    </group>
  );
}
```
Note on the glass orientation: the lathe profile runs neck (y=0) → apex (y=1.1) *upward*; a pendant bulb's apex points *down*, so the mesh is rotated π about X and positioned at the neck. Keep the filament group's y offset as written and correct it by eye in Task 9 if the blades sit outside the glass.

- [ ] **Step 2: Typecheck and lint**

Run: `pnpm exec tsc --noEmit && pnpm exec eslint src/components/landing/HeroLamp/Lamp.tsx`
Expected: clean. If TS complains about `shadow-mapSize-width` style props, they are valid R3F dashed props; ensure `@react-three/fiber` is imported in the file (it is, via `useFrame`) so its JSX augmentation loads.

- [ ] **Step 3: Commit**

```bash
git add src/components/landing/HeroLamp/Lamp.tsx
git commit -m "feat(landing): the lamp — ST64 glass, three-blade filament, spot for shadow + point for glow"
```

---

### Task 7: `Scene.tsx` — canvas, environment, wall, bloom, demand loop, quality ladder

**Files:**
- Create: `src/components/landing/HeroLamp/Scene.tsx`

**Interfaces:**
- Consumes: `Lamp`, `LAMP_ANCHOR_Y` (Task 6); `createPendulum`, `SCROLL_IMPULSE` (Task 2); `cssColor` (Task 5); `useLampTheme` (Task 1).
- Produces: `export default function Scene(): JSX.Element` — default export for `next/dynamic`.

- [ ] **Step 1: Write the scene**

`src/components/landing/HeroLamp/Scene.tsx`:
```tsx
"use client";

import * as React from "react";
import * as THREE from "three";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Environment, Lightformer, PerformanceMonitor } from "@react-three/drei";
import { Bloom, EffectComposer } from "@react-three/postprocessing";
import { useLampTheme } from "./lampStore";
import { SCROLL_IMPULSE, createPendulum } from "./pendulum";
import { cssColor } from "./lampGeometry";
import { Lamp, LAMP_ANCHOR_Y, type LampColors } from "./Lamp";

const STRIKE_MS = 700;
const CAMERA_Z = 8;
const CAMERA_FOV = 40;

type Quality = 2 | 1 | 0; // 2 = full, 1 = no bloom, 0 = no bloom + no shadows

function readColors(): LampColors {
  return {
    bulb: cssColor("--bulb", "#F0C34E"),
    brass: cssColor("--brass", "#B68235"),
    midnight: cssColor("--midnight", "#0F1A2B"),
    cream: cssColor("--cream", "#F5EFE2"),
  };
}

/** Wall whose emissive is the page colour — exact, untonemapped — so the
 *  canvas's bottom mask blends into the page. The lights add the pool. */
function Wall({ litRef, colors }: { litRef: React.MutableRefObject<number>; colors: LampColors }) {
  const mat = React.useRef<THREE.MeshStandardMaterial>(null);
  const midnight = React.useMemo(() => new THREE.Color(colors.midnight), [colors.midnight]);
  const cream = React.useMemo(() => new THREE.Color(colors.cream), [colors.cream]);
  useFrame(() => {
    if (!mat.current) return;
    const k = 1 - Math.pow(1 - litRef.current, 3);
    mat.current.emissive.copy(midnight).lerp(cream, k);
  });
  return (
    <mesh position={[0, 0, -1.6]} receiveShadow>
      <planeGeometry args={[60, 40]} />
      <meshStandardMaterial
        ref={mat}
        color={cream}
        emissive={midnight}
        emissiveIntensity={1}
        roughness={0.96}
        toneMapped={false}
      />
    </mesh>
  );
}

/** Drives the strike tween + pendulum, and keeps the demand loop alive only
 *  while something moves. */
function Driver({
  litRef,
  pendulum,
  visible,
}: {
  litRef: React.MutableRefObject<number>;
  pendulum: ReturnType<typeof createPendulum>;
  visible: boolean;
}) {
  const theme = useLampTheme();
  const invalidate = useThree((s) => s.invalidate);
  const target = theme === "light" ? 1 : 0;
  const startRef = React.useRef<{ from: number; at: number } | null>(null);

  React.useEffect(() => {
    startRef.current = { from: litRef.current, at: performance.now() };
    invalidate();
  }, [target, invalidate, litRef]);

  // Scroll + pointer nudge the pendulum; both request frames.
  React.useEffect(() => {
    let lastY = window.scrollY;
    let lastX: number | null = null;
    const onScroll = () => {
      const d = window.scrollY - lastY;
      lastY = window.scrollY;
      pendulum.impulse(d * SCROLL_IMPULSE);
      invalidate();
    };
    const onMove = (e: PointerEvent) => {
      const cx = window.innerWidth / 2;
      if (Math.abs(e.clientX - cx) < 140 && e.clientY < 260) {
        if (lastX !== null) pendulum.impulse((e.clientX - lastX) * 0.00004);
        invalidate();
      }
      lastX = e.clientX;
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("pointermove", onMove, { passive: true });
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("pointermove", onMove);
    };
  }, [pendulum, invalidate]);

  useFrame(() => {
    if (!visible) return;
    let busy = false;
    const s = startRef.current;
    if (s) {
      const t = Math.min(1, (performance.now() - s.at) / STRIKE_MS);
      litRef.current = s.from + (target - s.from) * t;
      if (t < 1) busy = true;
      else startRef.current = null;
    }
    pendulum.step();
    if (!pendulum.settled()) busy = true;
    if (busy) invalidate();
  });
  return null;
}

export default function Scene() {
  const litRef = React.useRef(0);
  const pendulum = React.useMemo(() => createPendulum(), []);
  const [colors] = React.useState(readColors);
  const [quality, setQuality] = React.useState<Quality>(2);
  const [dpr, setDpr] = React.useState<[number, number]>([1, 1.5]);
  const [visible, setVisible] = React.useState(true);
  const host = React.useRef<HTMLDivElement>(null);
  const narrow = typeof window !== "undefined" && window.innerWidth < 640;

  // Pause rendering entirely when the hero is off-screen.
  React.useEffect(() => {
    const el = host.current;
    if (!el) return;
    const io = new IntersectionObserver(([e]) => setVisible(e.isIntersecting), { threshold: 0 });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  const lit = useLampTheme() === "light";
  // Initial lit value: a returning visitor with a stored "light" should not
  // watch the strike replay on every load.
  React.useEffect(() => {
    litRef.current = lit ? 1 : 0;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div ref={host} className="ld-hero-canvas" aria-hidden="true">
      <Canvas
        frameloop={visible ? "demand" : "never"}
        dpr={dpr}
        shadows={quality > 0}
        gl={{ antialias: false, powerPreference: "high-performance" }}
        camera={{ position: [0, 0.4, CAMERA_Z], fov: CAMERA_FOV, near: 0.1, far: 40 }}
        onCreated={({ gl }) => {
          gl.toneMapping = THREE.ACESFilmicToneMapping;
        }}
      >
        <PerformanceMonitor
          onDecline={() => {
            setDpr(([lo]) => [lo, 1]);
            setQuality((q) => (q > 0 ? ((q - 1) as Quality) : q));
          }}
          onIncline={() => setDpr([1, 1.5])}
          flipflops={2}
        />
        <ambientLight color={colors.cream} intensity={0.03} />
        {/* Studio reflections, built in code — no CDN HDRI. Dim in the dark,
            brighter when lit, via environmentIntensity below. */}
        <Environment resolution={64} environmentIntensity={0.35 + 0.65 * litRef.current}>
          <Lightformer form="rect" intensity={1.4} color={colors.cream} scale={[6, 3]} position={[0, 5, 2]} target={[0, 0, 0]} />
          <Lightformer form="rect" intensity={0.5} color={colors.cream} scale={[3, 4]} position={[-5, 1, 2]} target={[0, 0, 0]} />
          <Lightformer form="circle" intensity={0.3} color={colors.bulb} scale={[2, 2]} position={[4, 2, 3]} target={[0, 0, 0]} />
        </Environment>

        <Wall litRef={litRef} colors={colors} />
        <Lamp
          lit={litRef.current}
          pendulum={pendulum}
          colors={colors}
          shadows={quality > 0}
          shadowMapSize={narrow ? 512 : 1024}
        />
        <Driver litRef={litRef} pendulum={pendulum} visible={visible} />

        {quality === 2 && (
          <EffectComposer multisampling={narrow ? 0 : 4}>
            <Bloom mipmapBlur luminanceThreshold={1} intensity={1.1} radius={0.6} />
          </EffectComposer>
        )}
      </Canvas>
    </div>
  );
}
```
Notes for the implementer:
- `Lamp`'s `lit` prop is read once; the per-frame value flows through `litRef` inside `Driver`/`Wall`. In `Lamp.tsx` the `litRef.current = lit` line means the prop only seeds it — that is fine because `Driver` writes the same shared ref object? **No** — they are different refs. Fix: pass `litRef` to `Lamp` instead of `lit`. Change `LampProps.lit: number` → `litRef: React.MutableRefObject<number>` and inside `useFrame` read `litRef.current`. Apply this change to `Lamp.tsx` in this task and update Task 6's interface accordingly (documented here so both files agree: **`Lamp` takes `litRef`**).
- `environmentIntensity` is read at render, not per frame; that is acceptable — it re-renders on theme change because `Scene` calls `useLampTheme()`.
- The camera looks along −Z at the origin; the lamp anchor at y=3.2 is above the visible top (visible half-height at z=0 ≈ `CAMERA_Z * tan(20°)` ≈ 2.9), so the cord enters from off-screen.

- [ ] **Step 2: Apply the `litRef` change to `Lamp.tsx`**

In `src/components/landing/HeroLamp/Lamp.tsx`: replace `lit: number;` with `litRef: React.MutableRefObject<number>;` in `LampProps`; change the destructure to `{ litRef, pendulum, colors, shadows, shadowMapSize }`; delete the two lines `const litRef = React.useRef(lit); litRef.current = lit;`. Everything else in `useFrame` already reads `litRef.current`.

- [ ] **Step 3: Typecheck and lint**

Run: `pnpm exec tsc --noEmit && pnpm exec eslint src/components/landing/HeroLamp`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add src/components/landing/HeroLamp
git commit -m "feat(landing): the room — canvas on demand, code-built environment, wall that turns cream, selective bloom"
```

---

### Task 8: `HeroLamp/index.tsx`, the page swap, and the DOM states

**Files:**
- Create: `src/components/landing/HeroLamp/index.tsx`
- Create: `src/components/landing/HeroLamp/webgl.ts`
- Modify: `src/app/page.tsx:3,26` (import + mount)
- Modify: `src/app/landing.css` (append the block below; adjust `.ld-funnel` padding)
- Test: `src/components/landing/HeroLamp/__tests__/index.test.tsx`

**Interfaces:**
- Consumes: `HangingLamp` (Task 3), `Scene` (Task 7), `useLampTheme`, `strike`, `hydrateFromStorage` (Task 1).
- Produces: `export function HeroLamp(): JSX.Element`; `export function hasWebGL(): boolean` (in `webgl.ts`).

- [ ] **Step 1: Write the failing test**

`src/components/landing/HeroLamp/__tests__/index.test.tsx`:
```tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

vi.mock("next/dynamic", () => ({
  default: () => {
    const Stub = () => <div data-testid="three-scene" />;
    return Stub;
  },
}));

import { HeroLamp } from "../index";
import { getTheme, setTheme } from "../lampStore";
import * as webgl from "../webgl";

function mockReducedMotion(reduce: boolean) {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: reduce && q.includes("prefers-reduced-motion"),
    media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

describe("HeroLamp chooses scene vs fallback", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setTheme("dark");
    vi.spyOn(webgl, "hasWebGL").mockReturnValue(true);
    mockReducedMotion(false);
  });
  afterEach(() => vi.restoreAllMocks());

  it("renders the three.js scene when WebGL is available and motion is allowed", () => {
    render(<HeroLamp />);
    expect(screen.getByTestId("three-scene")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /light/i, hidden: false })).toBeInTheDocument();
  });

  it("falls back to the SVG lamp under prefers-reduced-motion", () => {
    mockReducedMotion(true);
    render(<HeroLamp />);
    expect(screen.queryByTestId("three-scene")).not.toBeInTheDocument();
    expect(document.querySelector(".ld-lamp-svg")).not.toBeNull();
  });

  it("falls back to the SVG lamp without WebGL", () => {
    vi.spyOn(webgl, "hasWebGL").mockReturnValue(false);
    render(<HeroLamp />);
    expect(screen.queryByTestId("three-scene")).not.toBeInTheDocument();
    expect(document.querySelector(".ld-lamp-svg")).not.toBeNull();
  });

  it("the accessible switch over the glass strikes the lamp", () => {
    render(<HeroLamp />);
    fireEvent.click(screen.getByRole("button", { name: "Turn on the light" }));
    expect(getTheme()).toBe("light");
    expect(screen.getByRole("button", { name: "Turn off the light" })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm vitest run src/components/landing/HeroLamp/__tests__/index.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `webgl.ts` and `index.tsx`**

`src/components/landing/HeroLamp/webgl.ts`:
```ts
/** Feature-detect a usable WebGL context. Cheap, and it is the difference
 *  between a lit lamp and a blank rectangle on a locked-down machine. */
export function hasWebGL(): boolean {
  if (typeof document === "undefined") return false;
  try {
    const c = document.createElement("canvas");
    return !!(c.getContext("webgl2") || c.getContext("webgl"));
  } catch {
    return false;
  }
}
```

`src/components/landing/HeroLamp/index.tsx`:
```tsx
"use client";

/**
 * The hero lamp: a three.js pendant with a real light, or — on the server,
 * under prefers-reduced-motion, or without WebGL — the SVG lamp. Same store,
 * same contract, same triggers; only the rendering differs.
 *
 * The scene's canvas is not a control. The real control is the transparent
 * <button> positioned over the glass, so keyboard and screen-reader users
 * get "Turn on the light" like everyone else.
 */
import * as React from "react";
import dynamic from "next/dynamic";
import { HangingLamp } from "../HangingLamp";
import { hydrateFromStorage, strike, useLampTheme } from "./lampStore";
import { hasWebGL } from "./webgl";

const Scene = dynamic(() => import("./Scene"), { ssr: false, loading: () => null });

type Mode = "pending" | "scene" | "fallback";

export function HeroLamp() {
  const [mode, setMode] = React.useState<Mode>("pending");
  const theme = useLampTheme();

  React.useEffect(() => {
    hydrateFromStorage();
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    setMode(!reduce && hasWebGL() ? "scene" : "fallback");
    return () => document.documentElement.removeAttribute("data-lb-theme");
  }, []);

  if (mode === "fallback") return <HangingLamp />;

  return (
    <>
      {mode === "scene" && <Scene />}
      <button
        type="button"
        className="ld-lamp-switch"
        aria-label={theme === "dark" ? "Turn on the light" : "Turn off the light"}
        onClick={strike}
      />
    </>
  );
}

export default HeroLamp;
```
Note: `HangingLamp` also calls `hydrateFromStorage` and removes the attribute on unmount — harmless duplication; the store is idempotent.

- [ ] **Step 4: Mount it on the page**

In `src/app/page.tsx`: change line 3 to `import { HeroLamp } from "@/components/landing/HeroLamp";` and line 26 to `<HeroLamp />`. The hero section must be `position: relative` so the canvas can fill it — that is `.ld-funnel`, already `position: relative` (landing.css:2071).

**But** the canvas has to sit *inside* the hero section, and `page.tsx` mounts the lamp as a sibling *before* `<HeroFunnel />`. Move the mount: delete the `<HeroLamp />` line from `page.tsx` and instead render it as the first child inside `HeroFunnel`'s `<section className="ld-funnel" id="start">` — add `import { HeroLamp } from "./HeroLamp";` to `HeroFunnel.tsx` and insert `<HeroLamp />` immediately after the opening `<section …>` tag. Remove the `HangingLamp` import from `page.tsx` entirely.

- [ ] **Step 5: Append the DOM states to `landing.css`**

Append at the end of `src/app/landing.css`:
```css
/* ── The hero lamp (three.js) ─────────────────────────────────────────── */
/* The canvas fills the hero and fades into the page over its last 140px so
   the wall (whose emissive IS the page colour) and the page read as one
   surface. pointer-events none: the canvas is scenery; the control is the
   button below. */
.ld-hero-canvas {
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  -webkit-mask-image: linear-gradient(to bottom, #000 calc(100% - 140px), transparent 100%);
  mask-image: linear-gradient(to bottom, #000 calc(100% - 140px), transparent 100%);
}
.ld-hero-canvas canvas { display: block; width: 100% !important; height: 100% !important; }
.ld-funnel-in { position: relative; z-index: 1; }

/* The accessible switch over the glass. Transparent, but real: keyboard focus
   draws the ring where the bulb is. Vertical position tracks the lamp's
   world position (glass centre ≈ 21% down the section at fov 40). */
.ld-lamp-switch {
  position: absolute;
  left: 50%;
  top: calc(21% - 70px);
  width: 84px;
  height: 140px;
  margin-left: -42px;
  z-index: 2;
  background: transparent;
  border: 0;
  border-radius: 42px;
  cursor: pointer;
  padding: 0;
}
.ld-lamp-switch:focus-visible {
  outline: 2px solid var(--bulb);
  outline-offset: 4px;
}

/* DOM side of the two states. Dark = waiting (still ≥4.5:1); lit = reading. */
[data-lb-theme="dark"] .ld-funnel-h1 { color: rgba(245, 239, 226, 0.62); }
[data-lb-theme="dark"] .ld-ask .ld-btn {
  background: transparent;
  color: var(--cream);
  box-shadow: inset 0 0 0 1px var(--brass);
}
.ld-funnel-h1, .ld-ask .ld-btn {
  transition: color 0.6s cubic-bezier(0.22, 1, 0.36, 1),
              background-color 0.6s cubic-bezier(0.22, 1, 0.36, 1),
              box-shadow 0.6s ease;
}
@media (prefers-reduced-motion: reduce) {
  .ld-funnel-h1, .ld-ask .ld-btn { transition: none; }
}
```
Also change `.ld-funnel`'s `padding` (landing.css:2079, currently `clamp(210px, 27vh, 280px) 48px 56px`) to `clamp(230px, 30vh, 300px) 48px 56px` so the headline clears the 3D lamp's chain.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pnpm vitest run src/components/landing`
Expected: PASS — including the new `index.test.tsx` (4 tests) and every earlier landing test.

- [ ] **Step 7: Lint, typecheck, commit**

```bash
pnpm exec eslint src/components/landing src/app/page.tsx && pnpm exec tsc --noEmit
git add src/components/landing/HeroLamp src/components/landing/HeroFunnel.tsx src/app/page.tsx src/app/landing.css
git commit -m "feat(landing): mount the hero lamp — scene when it can, SVG when it must, one accessible switch"
```

---

### Task 9: Render verification and tuning (no new tests — evidence by screenshot)

**Files:**
- Modify (tuning only): `src/components/landing/HeroLamp/Lamp.tsx`, `Scene.tsx`, `src/app/landing.css`

- [ ] **Step 1: Start the dev server on a spare port**

Run from `apps/frontend/`: `pnpm dev --port 3100` (background). Wait until `curl -sf http://localhost:3100/ >/dev/null`.

- [ ] **Step 2: Render dark at 1440×900 and inspect**

Use the Playwright MCP: resize 1440×900, navigate `http://localhost:3100/`, clear localStorage first (`localStorage.clear()` via evaluate, then reload). Screenshot to `.playwright-mcp/lamp-dark-1440.png`. Read the image. Check: room is midnight; the glass is visible as an object (rim highlight); the filament is a dim ember; headline is dim cream; Start is a brass outline; nothing is yellow-filled; no second bulb.

- [ ] **Step 3: Strike and render lit**

Evaluate `document.querySelector('.ld-lamp-switch').click()`, wait 1s, screenshot `.playwright-mcp/lamp-lit-1440.png`. Check: filament hot with a halo (bloom), a warm pool on the wall directly under the lamp, the lamp's shadow on the wall, edges of the hero visibly softer than the centre, page background cream, Start filled `--bulb`, headline ink. If the pool blows out to white, lower `SPOT_INTENSITY`; if the filament is a flat yellow stick, raise `EMISSIVE_ON` or Bloom `intensity`; if the glass reads as a grey blob, raise `envMapIntensity` on the glass or the top Lightformer intensity.

- [ ] **Step 4: Verify the scene idles at 0 fps**

Evaluate:
```js
await new Promise(r => setTimeout(r, 3000)); // let the strike + swing settle
const start = performance.now(); let frames = 0;
await new Promise(r => { const tick = () => { frames++; if (performance.now() - start < 2000) requestAnimationFrame(tick); else r(); }; requestAnimationFrame(tick); });
frames // rAF count is the page's, not the canvas's — so instead:
```
Use R3F's own signal: add a temporary `console.log("frame")` inside `Driver`'s `useFrame`, reload, wait 3s after settling, and confirm no further "frame" logs appear for 3s (use the Playwright console tool). Remove the log afterwards. Record the observation in the commit message.

- [ ] **Step 5: Render at 390×844 (mobile), both states**

Resize to 390×844, repeat Steps 2–3 into `lamp-dark-390.png` / `lamp-lit-390.png`. Check the lamp does not collide with the eyebrow/headline and the switch button sits over the glass (evaluate `document.querySelector('.ld-lamp-switch').getBoundingClientRect()` and compare with the visible bulb).

- [ ] **Step 6: Contrast of the dim headline**

Evaluate `getComputedStyle(document.querySelector('.ld-funnel-h1')).color` in dark; compute contrast against `#0F1A2B` with the formula (WCAG relative luminance). Expected ≥ 4.5:1 (cream at 62% ≈ 6:1). If below, raise the alpha in `[data-lb-theme="dark"] .ld-funnel-h1`.

- [ ] **Step 7: Commit the tuned values with the numbers observed**

```bash
git add src/components/landing/HeroLamp src/app/landing.css
git commit -m "tune(landing): lamp intensities and bloom from render — <what changed and why, with the measured values>"
```
Show the four screenshots to the owner before proceeding to Task 10.

---

### Task 10: Full verification, docs cleanup, PR

**Files:**
- Delete: `docs/superpowers/specs/2026-08-28-hero-lamp-three-design.md`, `docs/superpowers/plans/2026-08-28-hero-lamp-three.md`

- [ ] **Step 1: Full frontend suite + lint + typecheck**

```bash
pnpm vitest run
pnpm exec eslint src
pnpm exec tsc --noEmit
```
Expected: all pass (vitest is RAM-heavy — run it alone, not alongside a dev server).

- [ ] **Step 2: Production build once**

Run: `pnpm build` (from `apps/frontend/`). Expected: succeeds; the route `/` first-load JS must not include three — confirm by grepping the build output for a separate chunk containing `three` (`grep -rl "LatheGeometry" .next/static/chunks | head -1` should be a chunk not listed under the `/` route's first-load set).

- [ ] **Step 3: Delete the planning docs (they shipped)**

```bash
git rm docs/superpowers/specs/2026-08-28-hero-lamp-three-design.md docs/superpowers/plans/2026-08-28-hero-lamp-three.md
git commit -m "docs: the hero lamp shipped — drop its spec and plan"
```

- [ ] **Step 4: Push and open the PR against `feat/lightbulb-rebrand`**

```bash
git push -u origin feat/hero-lamp-three
gh pr create --repo Isol8AI/isol8 --base feat/lightbulb-rebrand --head feat/hero-lamp-three \
  --title "feat(landing): the hero is a dark room — type an idea and the lamp comes on (three.js)" \
  --body-file - <<'EOF'
## What this is

The landing hero enacts its own headline. The room starts dark; the pendant lamp is off. Typing the first character of an idea — or clicking the lamp — strikes the filament and a real three.js light illuminates the room.

Stacked on #2090 (the palette + marks); base is `feat/lightbulb-rebrand`.

## How it works

- One store (`HeroLamp/lampStore.ts`) owns the theme and keeps the existing `data-lb-theme` + `lightbulb-theme` contract. Three readers: the scene, the SVG fallback, the hero's input. Initial state is dark.
- The lamp is the §02 anatomy in depth: ST64 lathe glass, three tapered blades tied twice, stepped collar. A `SpotLight` inside it casts the shadow (one shadow-map render); a shadowless `PointLight` does the omni glow. Bloom is selective by construction — only the filament carries `toneMapped={false}` + emissive > 1.
- The wall's emissive lerps midnight → cream on the strike so the room genuinely turns cream, warmest under the lamp; the canvas fades into the page over its last 140px.
- On demand: the canvas renders only while the pendulum has energy or the strike tween runs; paused off-screen; DPR ≤ 1.5; `PerformanceMonitor` ladder DPR → bloom → shadows.
- Fallback = the SVG lamp for SSR, reduced motion, no WebGL. Same triggers, same contract.

## Verification

- vitest: store, pendulum, geometry, fallback selection, keystroke → light, SVG lamp controls (unit, jsdom).
- Rendered: dark + lit at 1440 and 390 (screenshots in the PR thread); scene idles at 0 fps after settling; dim headline ≥ 4.5:1 on midnight.
- `pnpm build` — three ships in its own chunk, not in `/`'s first load.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
```

- [ ] **Step 5: Watch CI**

Arm a check-watch on the new PR (`gh pr checks <n> --json name,bucket` polled every 30s until none pending) and report each check as it lands. Required checks on this repo: CodeRabbit, Greptile Review, Lint & Build.

---

## Self-review (done while writing)

- **Spec coverage:** store/contract (T1), pendulum (T2), fallback (T3, T8), keystroke (T4), anatomy (T5, T6), lights split spot/point (T6), env map via Lightformers (T7), wall lerp + bottom mask (T7, T8), selective bloom (T7), demand loop + off-screen pause + DPR + quality ladder (T7), accessible switch (T8), DOM states + reduced-motion (T8), render + 0 fps + contrast checks (T9), build chunking + docs deletion + PR (T10). Gap found and closed: the `lit`-prop vs `litRef` mismatch between T6 and T7 — resolved in T7 Step 2 with the interface note.
- **Placeholders:** none — every code step is complete; tuning values in T6/T7 are declared starting values with the tuning procedure in T9.
- **Type consistency:** `createPendulum` / `Pendulum` (T2) used in T3, T6, T7; `useLampTheme`/`strike`/`lightOn`/`hydrateFromStorage` (T1) used in T3, T4, T7, T8; `cssColor` (T5) used in T7; `LAMP_ANCHOR_Y` (T6) used in T7; `hasWebGL` (T8) mocked by path in its test.
