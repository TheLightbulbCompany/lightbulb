"use client";

/**
 * The laptop on the desk — v3: it IS the stage. "her"/"hers" throughout this
 * file means Ksenia Kondrashova, whose CodePen `gOEgyaj` this model and its
 * rise-in timeline come from: the code is MIT under CodePen's terms, and the
 * GLB is her own unlicensed personal asset, self-hosted here at the owner's
 * explicit ruling. Deviations from hers are called out where they happen.
 *
 * Her open/screen-on timelines are GONE. The lid never closes (the hero's copy
 * lives on the screen), so there is nothing to choreograph on the strike: the
 * lid is fixed at LID_OPEN and the screen is lit from the first frame. Only her
 * rise-in survives.
 */
import * as React from "react";
import * as THREE from "three";
import { useFrame } from "@react-three/fiber";
import { useGLTF, useTexture } from "@react-three/drei";
import { RectAreaLightUniformsLib } from "three/examples/jsm/lights/RectAreaLightUniformsLib.js";
import type { LampColors } from "./Lamp";
import { DESK_TOP_Y } from "./Desk";

export const LAPTOP_URL = "/models/laptop/mac-noUv.glb";
const KEYS_URL = "/models/laptop/keyboard-overlay.png";

// ── Framing (tuned by render at 1440x900 and 1280x720; see Scene.tsx) ───────
// The model measures 30.5 wide x 21.5 deep in the base and 21 tall in the lid,
// so at 0.34 the laptop is 10.4 x 7.3 x 7.1 WORLD units — far bigger than the
// room it used to sit in, which is why the desk and the wall moved with it
// (Desk.tsx). The screen plane then projects 62.0% of the viewport width at
// 1440x900 and 66.2% at 1280x720; the GLB's black screen-frame overlaps its
// edges, so the LIT area measures ~2 points less — 60.1% and 64.1%, read off
// the rendered pixels just above the screen's bottom edge. Both readings are
// inside the spec's 60-70%, which is why this is 0.34 and not 0.33: at 0.33
// the lit area fell to 58.3% at 1440x900.
const SCALE = 0.34;
// Front-to-back seat on the desk: the hinge at z 1.0 puts the lid's top edge at
// z -3.49 (0.41 clear of the wall at -3.9) and the base's front lip at z 8.29,
// 0.41 short of the desk's front edge.
const LAPTOP_Z = 1.0;
const SCREEN: [number, number] = [29.4, 20];
const LID_OPEN = -0.2 * Math.PI; // hers
// three's WebGLLights extractRotation()s the light's world matrix, so the
// group's scale is normalised away and width/height are read as WORLD units —
// they have to be pre-scaled or the emitter is four desks wide. Constant now:
// the screen is never off, so there is nothing to tween.
const SCREEN_LIGHT = 6;

let rectInit = false;

export function Laptop({
  appearRef,
  colors,
  shadows,
}: {
  appearRef: React.MutableRefObject<number>;
  colors: LampColors;
  shadows: boolean;
}) {
  const { scene } = useGLTF(LAPTOP_URL);
  const keysTex = useTexture(KEYS_URL);
  const root = React.useRef<THREE.Group>(null);

  // Owner's exception: her materials, her hex. The only hex in the scene
  // besides the keyboard overlay's 0xffffff below — that one is an alpha
  // mask's carrier white, not a colour choice, so it stays at its mesh.
  const mats = React.useMemo(
    () => ({
      metal: new THREE.MeshStandardMaterial({ color: 0xcecfd3 }),
      dark: new THREE.MeshStandardMaterial({ color: 0x000000, roughness: 0.9, metalness: 0.9 }),
      cam: new THREE.MeshBasicMaterial({ color: 0x333333 }),
      logo: new THREE.MeshBasicMaterial({ color: 0xffffff }),
    }),
    [],
  );

  // drei's useGLTF cache is module-global and has no lifespan: every mount
  // gets the SAME gltf.scene. <primitive object={...}> REPARENTS what it is
  // handed, so mounting _top/_bottom straight from the cache empties the
  // cached scene, and the next mount (a client-side nav back to /, or Fast
  // Refresh) finds nothing and throws out of the Canvas. Clone per mount and
  // never touch the cached graph. clone(true) is enough here — no skinned
  // meshes; geometries stay shared and every material is assigned below.
  // Sharing them is safe on unmount without any opt-out: R3F v9's removeChild
  // skips disposal for `child.type === "primitive"` exactly so a <primitive>
  // never destroys an object it was only handed, so the cache's geometries
  // outlive this mount on their own.
  const model = React.useMemo(() => scene.clone(true), [scene]);

  // Split her model into lid (_top) and base (_bottom) groups.
  const parts = React.useMemo(
    () => ({
      top: model.getObjectByName("_top"),
      bottom: model.getObjectByName("_bottom"),
    }),
    [model],
  );
  const { top, bottom } = parts;

  // Materials are assigned by mesh NAME, in a layout effect rather than in
  // the memo above: painting the graph is a side effect and belongs after the
  // commit, not during a render that may be discarded (the GLB itself ships
  // no materials). useLayoutEffect, not useEffect, so no frame can be drawn
  // with the GLB's defaults. Swapping the model later is a model change only
  // — this table is the whole binding.
  React.useLayoutEffect(() => {
    if (!top || !bottom) {
      console.warn(`[Laptop] ${LAPTOP_URL} has no _top/_bottom node — laptop skipped.`);
      return;
    }
    const byName: Record<string, THREE.Material> = {
      lid: mats.metal,
      base: mats.metal,
      logo: mats.logo,
      camera: mats.cam,
      "screen-frame": mats.dark,
      legs: mats.dark,
      keyboard: mats.dark,
      inner: mats.dark,
    };
    for (const g of [top, bottom])
      g.traverse((o) => {
        const m = o as THREE.Mesh;
        if (!m.isMesh) return;
        m.material = byName[m.name] ?? mats.dark;
        m.castShadow = shadows;
        m.receiveShadow = m.name === "base";
      });
  }, [top, bottom, mats, shadows]);

  React.useEffect(() => {
    if (!rectInit) {
      RectAreaLightUniformsLib.init();
      rectInit = true;
    }
    return () => {
      Object.values(mats).forEach((m) => m.dispose());
    };
  }, [mats]);

  // her laptopAppearTl: from lying flat below the frame to seated on the desk.
  // The only thing left moving in this file. Driver owns the clock AND the
  // demand loop's `busy` flag while `appearRef` is under 1, so this never has
  // to ask for a frame of its own.
  useFrame(() => {
    const a = 1 - Math.pow(1 - appearRef.current, 3);
    if (!root.current) return;
    // Ends FLAT and face-on (hers ends pitched 0.05π — she floated it): on a
    // desk a 9° pitch sinks the front feet into the top, and the copy now
    // lives ON the screen, so any residual yaw skews it.
    root.current.rotation.x = 0.5 * Math.PI * (1 - a);
    root.current.rotation.y = 0.2 * Math.PI * (1 - a);
    root.current.position.y = DESK_TOP_Y - 2.2 * (1 - a);
  });

  if (!top || !bottom) return null;

  return (
    <group ref={root} position={[0, DESK_TOP_Y, LAPTOP_Z]} scale={SCALE}>
      <group rotation={[LID_OPEN, 0, 0]}>
        <primitive object={top} />
        {/* The glass the DOM copy will sit on (Task 16): a midnight plane that
            emits its own colour, untonemapped, so cream type on it reads the
            same in both rooms. A dark plane sits behind it for the same reason
            hers did — the lid's 0xCECFD3 shell must never show through.
            Hers sits 1mm back; at our scale that is 3e-4 world units — below
            the depth buffer's resolution out here, and it moiréd. 0.03 model
            units is still invisibly close and 100x the precision. */}
        <mesh position={[0, 10.5, -0.11]} rotation={[Math.PI, 0, 0]}>
          <planeGeometry args={SCREEN} />
          <meshStandardMaterial
            color={colors.midnight}
            emissive={colors.midnight}
            emissiveIntensity={1}
            side={THREE.BackSide}
            toneMapped={false}
          />
        </mesh>
        <mesh position={[0, 10.5, -0.14]} rotation={[Math.PI, Math.PI, 0]}>
          <planeGeometry args={SCREEN} />
          <primitive object={mats.dark} attach="material" />
        </mesh>
        <rectAreaLight
          color={colors.cream}
          intensity={SCREEN_LIGHT}
          width={SCREEN[0] * SCALE}
          height={SCREEN[1] * SCALE}
          position={[0, 10.5, 0]}
          rotation={[Math.PI, 0, 0]}
        />
      </group>
      <group>
        <primitive object={bottom} />
        <mesh rotation={[-0.5 * Math.PI, 0, 0]} position={[0, 0.045, 7.21]}>
          <planeGeometry args={[27.7, 11.6]} />
          <meshBasicMaterial color={0xffffff} alphaMap={keysTex} transparent />
        </mesh>
      </group>
    </group>
  );
}
