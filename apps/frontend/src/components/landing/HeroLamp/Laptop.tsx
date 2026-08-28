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
const SCALE = 0.055;
const SCREEN: [number, number] = [29.4, 20];
const LID_CLOSED = 0.5 * Math.PI,
  LID_OPEN = -0.2 * Math.PI; // hers

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
  const { scene: model } = useGLTF(LAPTOP_URL);
  const invalidate = useThree((s) => s.invalidate);
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

  // Split her model into lid (_top) and base (_bottom) groups.
  const parts = React.useMemo(
    () => ({
      top: model.getObjectByName("_top") as THREE.Group,
      bottom: model.getObjectByName("_bottom") as THREE.Group,
    }),
    [model],
  );

  // Materials are assigned by mesh NAME, in a layout effect rather than in
  // the memo above: the GLB scene is drei's module-global cache, so mutating
  // it is a side effect and belongs after the commit, not during render (the
  // GLB itself ships no materials). Swapping the model later is a model
  // change only — this table is the whole binding.
  React.useLayoutEffect(() => {
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
    for (const g of [parts.top, parts.bottom])
      g.traverse((o) => {
        const m = o as THREE.Mesh;
        if (!m.isMesh) return;
        m.material = byName[m.name] ?? mats.dark;
        m.castShadow = shadows;
        m.receiveShadow = m.name === "base";
      });
  }, [parts, mats, shadows]);

  React.useEffect(() => {
    if (!rectInit) {
      RectAreaLightUniformsLib.init();
      rectInit = true;
    }
    // Redraw the screen only when the idea changes; first paint after Lora is
    // ready. The repaint has to ask for a frame of its own: nothing else moves
    // when the idea changes (the lamp is already lit by the first keystroke),
    // and under frameloop="demand" a new texture is drawn by nobody.
    const redraw = () => {
      idea.draw(getIdeaText());
      invalidate();
    };
    const off = subscribe(redraw);
    document.fonts?.ready.then(redraw);
    return () => {
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
      root.current.rotation.x = 0.5 * Math.PI * (1 - a) + 0.05 * Math.PI * a;
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
    if (light.current) light.current.intensity = 1.5 * s;
  });

  return (
    <group ref={root} position={[0.55, DESK_TOP_Y, 0.4]} scale={SCALE}>
      <group ref={lid} rotation={[LID_CLOSED, 0, 0]}>
        <primitive object={parts.top} />
        {/* her screen: a plane in lid space, with a dark plane behind it for
            the off state (the texture only reaches 0.96 opacity, and the lid's
            0xCECFD3 shell showing through 4% would wash the midnight out).
            Hers sits 1mm back; at our 0.055 scale that is 5.5e-5 world units —
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
          width={SCREEN[0]}
          height={SCREEN[1]}
          position={[0, 10.5, 0]}
          rotation={[Math.PI, 0, 0]}
        />
      </group>
      <group>
        <primitive object={parts.bottom} />
        <mesh rotation={[-0.5 * Math.PI, 0, 0]} position={[0, 0.045, 7.21]}>
          <planeGeometry args={[27.7, 11.6]} />
          <meshBasicMaterial color={0xffffff} alphaMap={keysTex} transparent />
        </mesh>
      </group>
    </group>
  );
}
useGLTF.preload(LAPTOP_URL);
