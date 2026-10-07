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

/**
 * ST64 glass lathed from the logo's silhouette, a brass collar, the three-blade
 * Edison cage tied twice, and the idea inside as points. Plain three.js, no
 * React: the scene mounts the group with <primitive> and drives it per frame.
 */
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
  const neck = new THREE.Mesh(new THREE.CylinderGeometry(0.36, 0.34, 0.16, 64), brassMat);
  neck.position.y = -1.1;
  collar.add(neck);
  for (let i = 0; i < 4; i++) {
    const ring = new THREE.Mesh(new THREE.TorusGeometry(0.33, 0.045, 16, 64), brassMat);
    ring.rotation.x = Math.PI / 2;
    ring.position.y = -1.22 - i * 0.11;
    collar.add(ring);
  }
  const core = new THREE.Mesh(new THREE.CylinderGeometry(0.3, 0.26, 0.5, 64), brassMat);
  core.position.y = -1.4;
  collar.add(core);
  const tip = new THREE.Mesh(
    new THREE.SphereGeometry(0.11, 32, 16),
    new THREE.MeshStandardMaterial({ color: "#2a2622", metalness: 0.6, roughness: 0.5 }),
  );
  tip.position.y = -1.7;
  collar.add(tip);
  group.add(collar);

  const filament = new THREE.Group();
  const filMat = new THREE.MeshStandardMaterial({
    color: "#2b1e0c", emissive: GOLD, emissiveIntensity: 0, roughness: 0.6, metalness: 0.2,
  });
  for (let i = 0; i < 3; i++) {
    const a = (i / 3) * Math.PI * 2, dir = new THREE.Vector3(Math.cos(a), 0, Math.sin(a));
    const pts = [
      new THREE.Vector3(0, -0.88, 0).addScaledVector(dir, 0.05),
      new THREE.Vector3(0, -0.45, 0).addScaledVector(dir, 0.42),
      new THREE.Vector3(0, 0.25, 0).addScaledVector(dir, 0.55),
      new THREE.Vector3(0, 0.85, 0).addScaledVector(dir, 0.32),
      new THREE.Vector3(0, 1.12, 0).addScaledVector(dir, 0.02),
    ];
    const curve = new THREE.CatmullRomCurve3(pts, false, "catmullrom", 0.6);
    filament.add(new THREE.Mesh(new THREE.TubeGeometry(curve, 80, 0.011, 8, false), filMat));
  }
  for (const y of [-0.15, 0.6]) {
    const tie = new THREE.Mesh(new THREE.TorusGeometry(y < 0 ? 0.49 : 0.44, 0.006, 8, 96), filMat);
    tie.rotation.x = Math.PI / 2;
    tie.position.y = y;
    filament.add(tie);
  }
  const stem = new THREE.Mesh(new THREE.CylinderGeometry(0.04, 0.05, 0.4, 24), glassMat);
  stem.position.y = -0.95;
  filament.add(stem);
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

/** The bulb stands on something: a dark floor, semi-transparent so the mirrored bulb shows through as a reflection. */
export function buildFloor(): THREE.Mesh {
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(40, 40),
    new THREE.MeshStandardMaterial({ color: "#15120f", roughness: 0.85, metalness: 0, transparent: true, opacity: 0.72 }),
  );
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = FLOOR_Y;
  floor.renderOrder = -1;
  return floor;
}
