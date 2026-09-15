import { Host } from "@expo/ui";
import { Button, Form, HStack, Image, Spacer, Text, VStack } from "@expo/ui/swift-ui";
import {
  accessibilityAddTraits,
  accessibilityLabel,
  accessibilityRemoveTraits,
  font,
  foregroundStyle,
  frame,
} from "@expo/ui/swift-ui/modifiers";
import type { SFSymbol } from "expo-symbols";
import type { ReactNode } from "react";

import { colors, spacing } from "../../config/theme";

/**
 * A native grouped list — the iOS Settings look: SwiftUI's `Form` (@expo/ui)
 * filling the screen. Its children are SwiftUI `Section`s of rows.
 *
 * SwiftUI owns the scrolling, the grouped inset cards, the separators, the
 * row press highlight and the safe areas (the header above, the home
 * indicator or tab bar below), so a pushed screen needs no ScrollView of its
 * own. Rows draw in the system font, the same exemption `type.navTitle` makes
 * for the nav bar: an iOS list in a brand font reads as a web view.
 */
export function GroupedList({ children }: { children: ReactNode }) {
  return (
    // Pinned light like the rest of the app's palette. The seed colour is the
    // tint every plain row button (Retry, Manage subscription) draws in.
    <Host style={{ flex: 1 }} colorScheme="light" seedColor={colors.forest}>
      <Form>{children}</Form>
    </Host>
  );
}

const ICON = [font({ textStyle: "body" }), foregroundStyle(colors.forest), frame({ width: 28 })];
const TITLE = [foregroundStyle(colors.ink)];
const SUBTITLE = [font({ textStyle: "footnote" }), foregroundStyle(colors.mute)];
const CHEVRON = [font({ textStyle: "footnote", weight: "semibold" }), foregroundStyle(colors.muteSoft)];
const LINK_TRAITS = [accessibilityAddTraits(["isLink"]), accessibilityRemoveTraits(["isButton"])];

/**
 * A tappable row with an SF Symbol, a title, an optional subtitle and the
 * disclosure chevron. Inside a `GroupedList` section.
 */
export function NavigationRow({
  title,
  subtitle,
  icon,
  onPress,
  link = false,
}: {
  title: string;
  subtitle?: string;
  icon: SFSymbol;
  onPress: () => void;
  /** Opens a web page rather than pushing a screen, so VoiceOver says "link". */
  link?: boolean;
}) {
  return (
    <Button
      onPress={onPress}
      // The title alone, as the Pressable rows before it were labelled —
      // otherwise VoiceOver also reads the subtitle and the chevron.
      modifiers={link ? [accessibilityLabel(title), ...LINK_TRAITS] : [accessibilityLabel(title)]}
    >
      <HStack spacing={spacing.md}>
        {/* A fixed-width column, so every title starts at the same x. */}
        <Image systemName={icon} modifiers={ICON} />
        <VStack alignment="leading" spacing={2}>
          <Text modifiers={TITLE}>{title}</Text>
          {subtitle ? <Text modifiers={SUBTITLE}>{subtitle}</Text> : null}
        </VStack>
        <Spacer />
        <Image systemName="chevron.right" modifiers={CHEVRON} />
      </HStack>
    </Button>
  );
}
