import { Image } from "expo-image";
import { SymbolView } from "expo-symbols";

import { colors } from "../../config/theme";

// PNGs, not SVG: mobile has no react-native-svg, and adding it would change the
// native fingerprint and strand every OTA. Codex's mark is a template tinted ink.
const LOGOS: Record<string, number> = {
  claude: require("../../../assets/images/sessionTools/claude.png"),
  codex: require("../../../assets/images/sessionTools/codex.png"),
};

/** The coding tool a shared session comes from, as its logo. Hidden from VoiceOver:
 *  every call site names the tool in its own label. Unknown tools get a terminal. */
export function SessionToolIcon({ provider, size = 18 }: { provider: string; size?: number }) {
  const logo = LOGOS[provider];
  return logo ? (
    <Image testID={`session-tool-${provider}`} source={logo} style={{ width: size, height: size }} tintColor={provider === "codex" ? colors.ink : undefined}
      accessible={false} accessibilityElementsHidden importantForAccessibility="no-hide-descendants" />
  ) : (
    <SymbolView testID="session-tool-unknown" name="terminal" size={size} tintColor={colors.inkSoft} accessibilityElementsHidden />
  );
}
