/**
 * Brand wordmark for the onboarding / provisioning surfaces (mobile
 * onboarding-parity Slice 3). Mobile had no wordmark anywhere; web's
 * onboarding cards all lead with one (`<Logo size="lg" href={null}/>` at the
 * top of every ProvisioningStepper branch), which is what makes those screens
 * read as "Isol8 is setting things up" rather than an anonymous spinner.
 *
 * Text + casing mirror web's `Logo.tsx` (`<span …>ISOL8</span>`) — uppercase,
 * serif, forest. The letter-spacing is what keeps the uppercase reading as a
 * wordmark instead of a shouty heading.
 */
import { StyleSheet, Text } from "react-native";

import { colors, fonts } from "../../config/theme";

export function Wordmark() {
  return (
    <Text
      style={styles.wordmark}
      accessibilityRole="header"
      accessibilityLabel="Lightbulb AI"
    >
      ISOL8
    </Text>
  );
}

const styles = StyleSheet.create({
  wordmark: {
    fontFamily: fonts.lora,
    fontSize: 22,
    lineHeight: 30,
    letterSpacing: 1,
    color: colors.forest,
    textAlign: "center",
  },
});
