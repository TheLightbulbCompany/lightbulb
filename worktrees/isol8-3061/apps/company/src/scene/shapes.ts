import type { ShapeName } from "@/content";

type V3 = { x: number; y: number; z: number };
const v3 = (x: number, y: number, z: number): V3 => ({ x, y, z });
const dist = (a: V3, b: V3) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);

/**
 * The idea inside the bulb, as one point cloud with many shapes. Every generator
 * writes n points into a fresh Float32Array(n*3), centred near the origin, so the
 * scene can lerp between any two of them index-for-index.
 */
export function makeShapes(n: number, rand: () => number = Math.random): Record<ShapeName, Float32Array> {
  const rnd = (a: number, b: number) => a + rand() * (b - a);
  const gauss = () => (rand() + rand() + rand() - 1.5) * 0.8;
  const onSphere = () => {
    const u = rnd(-1, 1), ph = rnd(0, Math.PI * 2), s = Math.sqrt(1 - u * u);
    return v3(s * Math.cos(ph), u, s * Math.sin(ph));
  };
  const alloc = () => new Float32Array(n * 3);
  const put = (p: Float32Array, i: number, x: number, y: number, z: number) => {
    p[i * 3] = x; p[i * 3 + 1] = y; p[i * 3 + 2] = z;
  };

  // Hero — scattered dust in the dark, well outside the glass.
  const scatter = alloc();
  for (let i = 0; i < n; i++) {
    const r = 2.6 * Math.cbrt(rand()), d = onSphere();
    put(scatter, i, r * d.x, r * d.y * 0.7 + 0.2, r * d.z);
  }

  // The strike — a folded ellipsoid, split down the middle.
  const brain = alloc();
  {
    const a = 0.46, b = 0.38, c = 0.34;
    let i = 0;
    while (i < n) {
      const d = onSphere(), x = d.x, y = d.y, z = d.z;
      const f = 0.075 * Math.sin(9 * x + 3 * Math.sin(5 * z)) * Math.cos(8 * y + 2 * Math.cos(7 * x))
        + 0.045 * Math.sin(14 * z + 6 * y) * Math.cos(6 * x);
      const shell = rand() < 0.7 ? 1 : Math.pow(rand(), 0.45);   // ~70% surface, 30% volume
      const r = (1 + f) * shell, px = x * a * r, py = y * b * r, pz = z * c * r;
      if (Math.abs(px) < 0.028) continue;                         // sagittal fissure
      if (py < -0.22 && rand() < 0.55) continue;                  // flatter underside
      put(brain, i, px, py, pz);
      i++;
    }
  }

  // Lightbulb AI — a team of agents, wired together.
  const network = alloc();
  {
    const nodes: V3[] = [v3(0, 0, 0)];
    for (let k = 0; k < 14; k++) {
      const d = onSphere(), r = rnd(0.32, 0.5);
      nodes.push(v3(r * d.x, r * d.y, r * d.z));
    }
    const edges: [number, number][] = [];
    nodes.forEach((nd, i) => {
      nodes
        .map((m, j) => ({ j, d: dist(nd, m) }))
        .filter((e) => e.j !== i)
        .sort((a, b) => a.d - b.d)
        .slice(0, 2)
        .forEach((e) => edges.push([i, e.j]));
      if (i) edges.push([0, i]);
    });
    for (let i = 0; i < n; i++) {
      if (rand() < 0.35) {
        const nd = nodes[Math.floor(rand() * nodes.length)];
        put(network, i, nd.x + gauss() * 0.045, nd.y + gauss() * 0.045, nd.z + gauss() * 0.045);
      } else {
        const [a, b] = edges[Math.floor(rand() * edges.length)], t = rand();
        put(
          network, i,
          nodes[a].x + (nodes[b].x - nodes[a].x) * t + gauss() * 0.006,
          nodes[a].y + (nodes[b].y - nodes[a].y) * t + gauss() * 0.006,
          nodes[a].z + (nodes[b].z - nodes[a].z) * t + gauss() * 0.006,
        );
      }
    }
  }

  // GooseTown — a little skyline.
  const town = alloc();
  {
    // Corners stay under radius 0.7 with the ±0.035 jitter — the glass wall is at 0.72.
    const G = 9, cell = 0.1, base = -0.36, h: number[] = [];
    for (let x = 0; x < G; x++) {
      for (let z = 0; z < G; z++) {
        const d = Math.hypot(x - 4, z - 4) / 4;
        h.push(Math.max(0.04, rnd(0.08, 0.72) * (1 - d * 0.55)));
      }
    }
    for (let i = 0; i < n; i++) {
      const c = Math.floor(rand() * G * G), x = Math.floor(c / G), z = c % G, onTop = rand() < 0.25;
      put(town, i, (x - 4) * cell + rnd(-0.035, 0.035), base + (onTop ? h[c] : rand() * h[c]), (z - 4) * cell + rnd(-0.035, 0.035));
    }
  }

  // Ventures — a curve that compounds.
  const growth = alloc();
  {
    const bars = 8;
    for (let i = 0; i < n; i++) {
      const b = Math.floor(rand() * bars), x = -0.45 + b * (0.9 / (bars - 1));
      const hh = 0.08 + Math.pow(b / (bars - 1), 1.8) * 0.72, edge = rand() < 0.3;
      put(
        growth, i,
        x + (edge ? (rand() < 0.5 ? -0.04 : 0.04) : rnd(-0.04, 0.04)),
        -0.36 + (edge ? rand() * hh : rand() < 0.3 ? hh : rand() * hh),
        rnd(-0.04, 0.04),
      );
    }
  }

  // Physical AI — something that turns.
  const gear = alloc();
  {
    const teeth = 12;
    for (let i = 0; i < n; i++) {
      const r = rand();
      let x: number, y: number, z: number;
      if (r < 0.5) {
        const a = rnd(0, Math.PI * 2), t = rnd(0, Math.PI * 2), R = 0.4, tr = 0.06;
        x = (R + tr * Math.cos(t)) * Math.cos(a); y = (R + tr * Math.cos(t)) * Math.sin(a); z = tr * Math.sin(t);
      } else if (r < 0.88) {
        const k = Math.floor(rand() * teeth), a = (k / teeth) * Math.PI * 2 + rnd(-0.11, 0.11), rr = rnd(0.42, 0.56);
        x = rr * Math.cos(a); y = rr * Math.sin(a); z = rnd(-0.06, 0.06);
      } else {
        const a = rnd(0, Math.PI * 2), rr = rnd(0.08, 0.14);
        x = rr * Math.cos(a); y = rr * Math.sin(a); z = rnd(-0.05, 0.05);
      }
      put(gear, i, x, y + 0.02, z);
    }
  }

  return { scatter, brain, network, town, growth, gear };
}
