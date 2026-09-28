import { Text, type ViewStyle } from "react-native";

import { fonts, type ChatPalette } from "../../config/theme";

/**
 * The card slice one row of a grouped-inset section draws: side strokes
 * always, top corners on the first row, bottom corners on the last.
 */
export function insetRowStyle(palette: ChatPalette, index: number, count: number): ViewStyle {
  const first = index === 0;
  const last = index === count - 1;
  return {
    marginHorizontal: 16,
    backgroundColor: palette.surface,
    borderColor: palette.edge,
    borderLeftWidth: 1,
    borderRightWidth: 1,
    overflow: "hidden",
    ...(first ? { borderTopWidth: 1, borderTopLeftRadius: 12, borderTopRightRadius: 12 } : {}),
    ...(last ? { borderBottomWidth: 1, borderBottomLeftRadius: 12, borderBottomRightRadius: 12, marginBottom: 8 } : {}),
  };
}

/** 13pt semibold fgMute section title on the ground, 32pt left inset, header role, optional trailing count. */
export function InsetSectionHeader({ title, count, palette }: { title: string; count?: number; palette: ChatPalette }) {
  return (
    <Text
      accessibilityRole="header"
      style={{
        fontFamily: fonts.sansSemiBold,
        fontSize: 13,
        lineHeight: 18,
        color: palette.fgMute,
        paddingHorizontal: 32,
        paddingTop: 16,
        paddingBottom: 6,
        backgroundColor: palette.ground,
      }}
    >
      {title}
      {count != null ? (
        <>
          {"  "}
          <Text style={{ color: palette.fgMute }}>{count}</Text>
        </>
      ) : null}
    </Text>
  );
}
