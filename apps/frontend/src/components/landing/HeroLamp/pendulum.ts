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
