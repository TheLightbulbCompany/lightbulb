import type { SFSymbol } from "expo-symbols";
import { createContext, useContext, type ReactNode } from "react";
import { Pressable, ScrollView, Text, View } from "react-native";

import { chatPalette, fonts, spacing, type ChatPalette } from "../../config/theme";
import { SymbolView } from "./SymbolView";

/**
 * Android's grouped list: a ScrollView on the app's ground holding the
 * inset-card `Section`s from ./swiftui (iOS draws SwiftUI's `Form` instead;
 * see GroupedList.tsx). Same exports, same props.
 */
const GroupedPaletteContext = createContext<ChatPalette>(chatPalette.light);

export function useGroupedPalette(): ChatPalette {
  return useContext(GroupedPaletteContext);
}

export function GroupedList({
  children,
  colorScheme = "light",
}: {
  children: ReactNode;
  colorScheme?: "light" | "dark";
}) {
  const palette = chatPalette[colorScheme];
  return (
    <GroupedPaletteContext.Provider value={palette}>
      <ScrollView style={{ flex: 1, backgroundColor: palette.ground }} contentContainerStyle={{ paddingBottom: spacing.xl }}>
        {children}
      </ScrollView>
    </GroupedPaletteContext.Provider>
  );
}

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
  link?: boolean;
}) {
  const p = useGroupedPalette();
  return (
    <Pressable
      accessibilityRole={link ? "link" : "button"}
      accessibilityLabel={title}
      onPress={onPress}
      style={({ pressed }) => ({
        flexDirection: "row",
        alignItems: "center",
        gap: spacing.md,
        minHeight: 56,
        paddingHorizontal: 16,
        paddingVertical: 10,
        backgroundColor: pressed ? p.selected : undefined,
      })}
    >
      <View style={{ width: 28, alignItems: "center" }}>
        <SymbolView name={icon} size={22} tintColor={p.link} />
      </View>
      <View style={{ flex: 1, gap: 2 }}>
        <Text style={{ fontFamily: fonts.sans, fontSize: 17, color: p.fg }}>{title}</Text>
        {subtitle ? <Text style={{ fontFamily: fonts.sans, fontSize: 13, color: p.fgMute }}>{subtitle}</Text> : null}
      </View>
      <SymbolView name="chevron.right" size={18} tintColor={p.fgMute} />
    </Pressable>
  );
}
