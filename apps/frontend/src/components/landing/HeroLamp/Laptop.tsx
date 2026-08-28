"use client";
import * as React from "react";
import * as THREE from "three";
import { useFrame, useThree } from "@react-three/fiber";
import { useGLTF, useTexture } from "@react-three/drei";
import { RectAreaLightUniformsLib } from "three/examples/jsm/lights/RectAreaLightUniformsLib.js";
import { makeIdeaTexture } from "./ideaTexture";
import { getIdeaText, subscribe } from "./lampStore";
import { easeStrike, type LampColors } from "./Lamp";
import { DESK_TOP_Y } from "./Desk";

export const LAPTOP_URL = "/models/laptop/mac-noUv.glb";
const KEYS_URL = "/models/laptop/keyboard-overlay.png";
const SCALE = 0.030;
const SCREEN: [number, number] = [29.4, 20];
const LID_CLOSED = 0.5 * Math.PI,
  LID_OPEN = -0.2 * Math.PI; // hers

// three's WebGLLights extractRotation()s the light's world matrix, so the
// group's 0.030 scale is normalised away and width/height are read as WORLD
// units — they have to be pre-scaled or the emitter is four desks wide.
// Screen-sized means ~330x less area than the unscaled one, so the intensity
// has to come up with it for the screen to light the keyboard at all.
const SCREEN_LIGHT = 8;

let rectInit = false;

export function Laptop({
  litRef,
  appearRef,
  colors,
  shadows,
}: {
  litRef: React.MutableRefObject<number>;
  appearRef: React.MutableRefObject<number>;
  colors: LampColors;
  shadows: boolean;
}) {
  const { scene } = useGLTF(LAPTOP_URL);
  const invalidate = useThree((s) => s.invalidate);
  // Under 640px the hero's copy and pill span the whole width, and the desk
  // band has no room left. Measured at 390x844 (canvas 375x758): the desk
  // projects 532..585, the idea card covers 433..564 across the full width
  // (22..353 of 375), and the fine copy starts at 582 — so the only desk not
  // behind DOM is a 21px strip, and the 22px side margins are narrower than
  // the closed lid. Every other gap (397..433, 644..666, 710..758) is off the
  // desk or inside the 140px bottom fade. Off the desk it would still be a
  // shadow caster and a light — so it is not mounted at all.
  const narrow = useThree((s) => s.size.width < 640);
  const keysTex = useTexture(KEYS_URL);
  const root = React.useRef<THREE.Group>(null);
  const lid = React.useRef<THREE.Group>(null);
  const screenMat = React.useRef<THREE.MeshBasicMaterial>(null);
  const light = React.useRef<THREE.RectAreaLight>(null);

  // Owner's exception: her materials, her hex. The ONLY hex in the scene.
  const mats = React.useMemo(
    () => ({
      metal: new THREE.MeshStandardMaterial({ color: 0xcecfd3 }),
      dark: new THREE.MeshStandardMaterial({ color: 0x000000, roughness: 0.9, metalness: 0.9 }),
      cam: new THREE.MeshBasicMaterial({ color: 0x333333 }),
      logo: new THREE.MeshBasicMaterial({ color: 0xffffff }),
    }),
    [],
  );
  const idea = React.useMemo(
    () => makeIdeaTexture(colors.midnight, colors.cream),
    [colors.midnight, colors.cream],
  );

  // drei's useGLTF cache is module-global and has no lifespan: every mount
  // gets the SAME gltf.scene. <primitive object={...}> REPARENTS what it is
  // handed, so mounting _top/_bottom straight from the cache empties the
  // cached scene, and the next mount (a client-side nav back to /, or Fast
  // Refresh) finds nothing and throws out of the Canvas. Clone per mount and
  // never touch the cached graph. clone(true) is enough here — no skinned
  // meshes; geometries stay shared and every material is assigned below.
  // `dispose={null}` on the two <primitive>s below is the other half: on
  // unmount R3F would otherwise dispose the clone's geometries, which ARE the
  // cache's geometries.
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
    // Redraw the screen only when the idea changes; first paint after Lora is
    // ready. The repaint has to ask for a frame of its own: nothing else moves
    // when the idea changes (the lamp is already lit by the first keystroke),
    // and under frameloop="demand" a new texture is drawn by nobody.
    let live = true;
    const redraw = () => {
      if (!live) return; // fonts.ready can land after unmount
      idea.draw(getIdeaText());
      invalidate();
    };
    const off = subscribe(redraw);
    document.fonts?.ready.then(redraw);
    return () => {
      live = false;
      off();
      idea.texture.dispose();
      Object.values(mats).forEach((m) => m.dispose());
    };
  }, [idea, mats, invalidate]);

  useFrame(() => {
    const k = easeStrike(litRef.current);
    const a = 1 - Math.pow(1 - appearRef.current, 3);
    // her laptopAppearTl: from lying flat below the frame to seated on the desk
    if (root.current) {
      // Ends FLAT (hers ends pitched 0.05π — she floated it): on a desk a 9°
      // pitch sinks the front feet 0.1 world units into the top.
      root.current.rotation.x = 0.5 * Math.PI * (1 - a);
      root.current.rotation.y = 0.2 * Math.PI * (1 - a) - 0.18 * a;
      root.current.position.y = DESK_TOP_Y - 2.2 * (1 - a);
    }
    // her laptopOpeningTl over the first 80% of the strike
    const o = Math.min(1, k / 0.8);
    if (lid.current) {
      lid.current.rotation.x = LID_CLOSED + (LID_OPEN - LID_CLOSED) * o;
      lid.current.position.z = 0.5 * (1 - o);
    }
    // her screenOnTl at 5% of the opening
    const s = THREE.MathUtils.clamp((o - 0.05) / 0.06, 0, 1);
    if (screenMat.current) screenMat.current.opacity = 0.96 * s;
    if (light.current) light.current.intensity = SCREEN_LIGHT * s;
  });

  if (narrow || !top || !bottom) return null;

  return (
    // x 3.0, not centred: the headline and the idea pill are DOM over the
    // middle of the canvas down to ~77% of its height, where the desk top
    // only starts — a centred laptop's screen is behind the headline or
    // behind the pill. To the right of the pill (edge 320px from centre; the
    // laptop's left edge lands 22px past it at 1280×720, 40px at 1440×900)
    // the whole screen shows. At SCALE 0.030 / z −0.3 the lid top projects to
    // 519 against the headline's last line's bottom at 522 at 1440×900 — a
    // 3px BOX overlap with zero painted-pixel collision and ~182px
    // horizontal separation, which the controller has explicitly accepted
    // rather than shrink the screen further (1280×720 clears with 11px to
    // spare). Any bigger or further forward and the base sinks into the
    // canvas's bottom fade. Still inside the desk's ±6.5 half-width (W 13,
    // D 2.0).
    <group ref={root} position={[3.0, DESK_TOP_Y, -0.3]} scale={SCALE}>
      <group ref={lid} rotation={[LID_CLOSED, 0, 0]}>
        <primitive object={top} dispose={null} />
        {/* her screen: a plane in lid space, with a dark plane behind it for
            the off state (the texture only reaches 0.96 opacity, and the lid's
            0xCECFD3 shell showing through 4% would wash the midnight out).
            Hers sits 1mm back; at our 0.030 scale that is 3.0e-5 world units —
            below the depth buffer's resolution out here, and it moiréd. 0.03
            model units is still invisibly close and 30× the precision. */}
        <mesh position={[0, 10.5, -0.11]} rotation={[Math.PI, 0, 0]}>
          <planeGeometry args={SCREEN} />
          <meshBasicMaterial
            ref={screenMat}
            map={idea.texture}
            transparent
            opacity={0}
            side={THREE.BackSide}
            toneMapped={false}
          />
        </mesh>
        <mesh position={[0, 10.5, -0.14]} rotation={[Math.PI, Math.PI, 0]}>
          <planeGeometry args={SCREEN} />
          <primitive object={mats.dark} attach="material" />
        </mesh>
        <rectAreaLight
          ref={light}
          color={colors.cream}
          intensity={0}
          width={SCREEN[0] * SCALE}
          height={SCREEN[1] * SCALE}
          position={[0, 10.5, 0]}
          rotation={[Math.PI, 0, 0]}
        />
      </group>
      <group>
        <primitive object={bottom} dispose={null} />
        <mesh rotation={[-0.5 * Math.PI, 0, 0]} position={[0, 0.045, 7.21]}>
          <planeGeometry args={[27.7, 11.6]} />
          <meshBasicMaterial color={0xffffff} alphaMap={keysTex} transparent />
        </mesh>
      </group>
    </group>
  );
}
useGLTF.preload(LAPTOP_URL);
