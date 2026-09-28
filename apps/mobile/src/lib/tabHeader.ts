import { useNavigation, useRoute, type NativeStackNavigationProp } from "expo-router";
import { useNavigationState, type ParamListBase } from "expo-router/react-navigation";
import { useLayoutEffect, useRef } from "react";

/** The (app) Stack's own options type, derived so no extra package import is needed. */
export type TabHeaderOptions = Parameters<NativeStackNavigationProp<ParamListBase>["setOptions"]>[0];

/**
 * The four tab roots are ONE screen of the (app) Stack and share its header, so
 * a tab writes its own header items there — keyed on the tab bar's SELECTION,
 * not on focus: a screen pushed over the tabs blurs the tab too, and stripping
 * the header then would pull the search bar out of the outgoing nav bar
 * mid-push. Options are read once per selection (through a ref), so every
 * closure in them must only call stable setters.
 */
export function useTabHeader(options: TabHeaderOptions, onLeave?: () => void): void {
  const header = useNavigation<NativeStackNavigationProp<ParamListBase>>("/(app)");
  const { key } = useRoute();
  const selected = useNavigationState((state) => state.routes[state.index]?.key === key);
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const leaveRef = useRef(onLeave);
  leaveRef.current = onLeave;
  useLayoutEffect(() => {
    if (!selected) return;
    const set = optionsRef.current;
    header.setOptions(set);
    return () => {
      leaveRef.current?.();
      header.setOptions(Object.fromEntries(Object.keys(set).map((name) => [name, undefined])));
    };
  }, [header, selected]);
}
