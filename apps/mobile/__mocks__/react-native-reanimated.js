// Manual jest mock for react-native-reanimated, loaded via the one-line
// `jest.mock("react-native-reanimated")` in jest-setup.ts.
//
// NOT the package's own shipped `react-native-reanimated/mock`: that file's
// mock.ts imports real values (advanceAnimationByTime, setUpTests, ...) from
// reanimated's own `./index`, which — on this installed version (4.5.0 +
// react-native-worklets 0.10.0) — unconditionally initializes the native
// worklets runtime at import time and throws ("Cannot read properties of
// undefined (reading 'loadUnpackers')") in a jest/node environment. Requiring
// the REAL (unmocked) package hits the same init path too.
//
// `createAnimatedComponent` and `useEvent` exist only because
// react-native-gesture-handler's own `reanimatedWrapper.ts` probes for them
// (`Reanimated.useSharedValue`/`.default.createAnimatedComponent`) whenever
// reanimated is installed at all, regardless of whether a given test uses
// gesture-handler's reanimated integration — BottomSheet.tsx never calls
// either directly. Extend this mock, not this comment, if a sibling branch's
// reanimated usage needs more of the surface.
const { View } = require("react-native");

function useSharedValue(initial) {
  const sv = { value: initial };
  sv.get = () => sv.value;
  sv.set = (v) => { sv.value = v; };
  return sv;
}

function useReducedMotion() {
  return false;
}

function useAnimatedStyle(factory) {
  return factory();
}

function withSpring(toValue, _config, callback) {
  callback?.(true);
  return toValue;
}

function withTiming(toValue, _config, callback) {
  callback?.(true);
  return toValue;
}

function interpolate(value, inputRange, outputRange) {
  const [x0, x1] = inputRange;
  const [y0, y1] = outputRange;
  if (x1 === x0) return y0;
  const t = Math.max(0, Math.min(1, (value - x0) / (x1 - x0)));
  return y0 + t * (y1 - y0);
}

// Endpoint-accurate only: RoomComposer's focus ring springs between exactly
// two colours, and a test asserts WHICH end a progress lands on — never the
// blend halfway between them (the real one parses and mixes colour channels).
// Give it a real mixer if a suite ever needs a midpoint.
function interpolateColor(value, inputRange, outputRange) {
  const index = inputRange.findIndex((stop) => value <= stop);
  return outputRange[index === -1 ? outputRange.length - 1 : index];
}

const Extrapolation = { CLAMP: "clamp", EXTEND: "extend", IDENTITY: "identity" };

// Identity: the real `runOnJS` marshals a worklet-thread call back to the JS
// thread; there is only one thread in this mock, so calling straight through
// is behaviorally equivalent.
function runOnJS(fn) {
  return fn;
}

// Identity: real usage wraps a component to forward animated style updates
// imperatively; nothing here needs that, so returning the component as-is is
// behaviorally equivalent for a test.
function createAnimatedComponent(Component) {
  return Component;
}

function useEvent() {
  return undefined;
}

module.exports = {
  __esModule: true,
  default: { View, createAnimatedComponent },
  useSharedValue,
  useReducedMotion,
  useAnimatedStyle,
  withSpring,
  withTiming,
  interpolate,
  interpolateColor,
  Extrapolation,
  runOnJS,
  useEvent,
};
