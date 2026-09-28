import { Host } from "@expo/ui";
import { Button, Form, HStack, Image, Spacer, Text, VStack } from "@expo/ui/swift-ui";
import {
  accessibilityAddTraits,
  accessibilityLabel,
  accessibilityRemoveTraits,
  background,
  font,
  foregroundStyle,
  frame,
  scrollContentBackground,
} from "@expo/ui/swift-ui/modifiers";
import type { SFSymbol } from "expo-symbols";
import { createContext, useContext, type ReactNode } from "react";

import { chatPalette, spacing, type ChatPalette } from "../../config/theme";

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
const GroupedPaletteContext = createContext<ChatPalette>(chatPalette.light);

/** The `chatPalette` half of the nearest `GroupedList`, or light outside one. */
export function useGroupedPalette(): ChatPalette {
  return useContext(GroupedPaletteContext);
}

export function GroupedList({
  children,
  colorScheme = "light",
}: {
  children: ReactNode;
  /** Which half of `chatPalette` the list — and its rows — draw from. */
  colorScheme?: "light" | "dark";
}) {
  const palette = chatPalette[colorScheme];
  return (
    // The seed colour is the tint every plain row button (Retry, Manage
    // subscription) draws in.
    <Host style={{ flex: 1 }} colorScheme={colorScheme} seedColor={palette.fg}>
      <GroupedPaletteContext.Provider value={palette}>
        {/* The app's own ground, not the system's grouped background: in dark
            that one is pure black, a different surface from every other screen
            and from the header above it. */}
        <Form modifiers={[scrollContentBackground("hidden"), background(palette.ground)]}>{children}</Form>
      </GroupedPaletteContext.Provider>
    </Host>
  );
}

const LINK_TRAITS = [accessibilityAddTraits(["isLink"]), accessibilityRemoveTraits(["isButton"])];

/**
 * A tappable row with an SF Symbol, a title, an optional subtitle and the
 * disclosure chevron. Inside a `GroupedList` section. Its colours follow the
 * enclosing `GroupedList`'s `colorScheme`.
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
  const p = useGroupedPalette();
  const iconMod = [font({ textStyle: "body" }), foregroundStyle(p.link), frame({ width: 28 })];
  const titleMod = [foregroundStyle(p.fg)];
  const subtitleMod = [font({ textStyle: "footnote" }), foregroundStyle(p.fgMute)];
  const chevronMod = [font({ textStyle: "footnote", weight: "semibold" }), foregroundStyle(p.fgMute)];
  return (
    <Button
      onPress={onPress}
      // The title alone, as the Pressable rows before it were labelled —
      // otherwise VoiceOver also reads the subtitle and the chevron.
      modifiers={link ? [accessibilityLabel(title), ...LINK_TRAITS] : [accessibilityLabel(title)]}
    >
      <HStack spacing={spacing.md}>
        {/* A fixed-width column, so every title starts at the same x. */}
        <Image systemName={icon} modifiers={iconMod} />
        <VStack alignment="leading" spacing={2}>
          <Text modifiers={titleMod}>{title}</Text>
          {subtitle ? <Text modifiers={subtitleMod}>{subtitle}</Text> : null}
        </VStack>
        <Spacer />
        <Image systemName="chevron.right" modifiers={chevronMod} />
      </HStack>
    </Button>
  );
}
