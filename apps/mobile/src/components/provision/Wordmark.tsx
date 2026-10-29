/**
 * Brand wordmark for the onboarding / provisioning surfaces (mobile
 * onboarding-parity Slice 3). Mobile had no wordmark anywhere; web's
 * onboarding cards all lead with one (`<Logo size="lg" href={null}/>` at the
 * top of every ProvisioningStepper branch), which is what makes those screens
 * read as "Lightbulb is setting things up" rather than an anonymous spinner.
 *
 * Text, casing and tracking mirror web's `Logo.tsx` / `.brand-logo`
 * (`<span …>LIGHTBULB</span>`) — uppercase Satoshi Medium in midnight, tracked
 * +0.18em (3.96 at 22px). §03: weight below 400 is never used, and 700 is not
 * the spec — 500 is.
 */
import { StyleSheet, Text } from "react-native";

import { colors, fonts } from "../../config/theme";

export function Wordmark() {
  return (
    <Text
      style={styles.wordmark}
      accessibilityRole="header"
      accessibilityLabel="Lightbulb"
    >
      LIGHTBULB
    </Text>
  );
}

const styles = StyleSheet.create({
  wordmark: {
    fontFamily: fonts.loraMedium,
    fontSize: 22,
    lineHeight: 30,
    letterSpacing: 3.96,
    color: colors.forest,
    textAlign: "center",
  },
});
