import { Image } from "expo-image";
import { SymbolView } from "../ui/SymbolView";

import { colors } from "../../config/theme";

// PNGs, not SVG: mobile has no react-native-svg, and adding it would change the
// native fingerprint and strand every OTA. Codex's mark is a template tinted ink.
const LOGOS: Record<string, number> = {
  claude: require("../../../assets/images/sessionTools/claude.png"),
  codex: require("../../../assets/images/sessionTools/codex.png"),
};

/** The coding tool a shared session comes from, as its logo. Hidden from VoiceOver:
 *  every call site names the tool in its own label. Unknown tools get a terminal.
 *  `tint` overrides the codex template + the unknown glyph only when passed --
 *  existing callers keep today's `ink` / `inkSoft` defaults. */
export function SessionToolIcon({ provider, size = 18, tint }: { provider: string; size?: number; tint?: string }) {
  const logo = LOGOS[provider];
  return logo ? (
    <Image testID={`session-tool-${provider}`} source={logo} style={{ width: size, height: size }} tintColor={provider === "codex" ? (tint ?? colors.ink) : undefined}
      accessible={false} accessibilityElementsHidden importantForAccessibility="no-hide-descendants" />
  ) : (
    <SymbolView testID="session-tool-unknown" name="terminal" size={size} tintColor={tint ?? colors.inkSoft} accessibilityElementsHidden />
  );
}
