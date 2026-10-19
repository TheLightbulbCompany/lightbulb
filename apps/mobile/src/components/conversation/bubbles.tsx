import type { ReactNode } from "react";
import { Animated, Pressable, StyleSheet, Text, View } from "react-native";
import { SymbolView } from "../ui/SymbolView";
import type { SFSymbol } from "expo-symbols";

import type { ChatPalette } from "../../config/theme";
import { fonts, spacing } from "../../config/theme";
import { usePulse } from "../chat/usePulse";

/** Muse-style bubble radius (Global Constraints: "radius 18" on both bubble shapes). */
export const BUBBLE_RADIUS = 18;
/** 28pt avatar + 8pt gap: where a collapsed (no-avatar) bubble starts, so its
 * body lines up with the first bubble of the run. */
export const AVATAR_GUTTER = 36;

/**
 * The one bubble shape: mine — `fg` fill, right-aligned, 78% wide. Others —
 * `surface` fill with a 1px `edge` stroke, left-aligned, 94% wide. A message
 * that `@mentions` the viewer strokes in `link` blue instead.
 *
 * A plain View, never a Pressable: the enclosing message row owns the long
 * press, and a Pressable here would win the responder and swallow it.
 */
export function Bubble(props: {
  mine: boolean;
  palette: ChatPalette;
  children: ReactNode;
  /** A message that @mentions the viewer: the stroke turns link-blue (web's row tint, as a border). */
  mentionsMe?: boolean;
  dimmed?: boolean;
  accessibilityLabel: string;
  testID?: string;
}) {
  const { mine, palette, children, mentionsMe, dimmed, accessibilityLabel, testID } = props;
  return (
    <View
      testID={testID}
      accessible
      accessibilityLabel={accessibilityLabel}
      style={[
        styles.bubble,
        mine
          ? { backgroundColor: palette.fg, alignSelf: "flex-end", maxWidth: "78%", paddingVertical: 8 }
          : {
              backgroundColor: palette.surface,
              borderColor: mentionsMe ? palette.link : palette.edge,
              borderWidth: 1,
              alignSelf: "flex-start",
              maxWidth: "94%",
              paddingVertical: 9,
            },
        dimmed && styles.dimmed,
      ]}
    >
      {children}
    </View>
  );
}

/** A wrapping row of hanging meta (chips, timestamp, read state) under a bubble. */
export function BubbleMeta(props: {
  align: "start" | "end";
  palette: ChatPalette;
  children: ReactNode;
  indent?: number;
}) {
  const { align, children, indent } = props;
  return (
    <View
      style={[
        styles.meta,
        { alignSelf: align === "end" ? "flex-end" : "flex-start" },
        indent !== undefined && { marginLeft: indent },
      ]}
    >
      {children}
    </View>
  );
}

/** Small hanging text — metadata, timestamp, read state, an unprompted label. */
export function MetaText(props: {
  palette: ChatPalette;
  children: string;
  tone?: "mute" | "alert" | "link";
  onPress?: () => void;
  accessibilityLabel?: string;
  /** Mirrors Chip's `expanded`: set it when this button discloses/hides
   *  content (a "show output"/"collapse" toggle), omit it otherwise. */
  expanded?: boolean;
}) {
  const { palette, children, tone = "mute", onPress, accessibilityLabel, expanded } = props;
  const color = tone === "alert" ? palette.alert : tone === "link" ? palette.link : palette.fgMute;
  const text = <Text style={[styles.metaText, { color }]}>{children}</Text>;
  if (!onPress) return text;
  return (
    <Pressable
      onPress={onPress}
      hitSlop={14}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      accessibilityState={expanded !== undefined ? { expanded } : undefined}
    >
      {text}
    </Pressable>
  );
}

/**
 * A pill hanging off a bubble: tool-run chip, sub-agent chip, "Thought" chip,
 * a reaction the viewer applied. Never a bubble itself.
 */
export function Chip(props: {
  label: string;
  palette: ChatPalette;
  icon?: SFSymbol;
  onPress?: () => void;
  expanded?: boolean;
  accessibilityLabel?: string;
  selected?: boolean;
}) {
  const { label, palette, icon, onPress, expanded, accessibilityLabel, selected } = props;
  const content = (
    <View
      style={[
        styles.chip,
        { backgroundColor: palette.chip },
        selected && { borderWidth: 1, borderColor: palette.fg },
      ]}
    >
      {icon && (
        <SymbolView accessibilityElementsHidden name={icon} size={11} tintColor={palette.fgSoft} />
      )}
      <Text style={[styles.chipLabel, { color: palette.fgSoft }]} numberOfLines={2}>
        {label}
      </Text>
      {/* An arrow says the chip opens something. A reaction is a toggle: it has
          a `selected` state and goes nowhere. */}
      {onPress && selected === undefined && (
        <SymbolView
          accessibilityElementsHidden
          name={expanded ? "chevron.down" : "chevron.right"}
          size={9}
          tintColor={palette.fgSoft}
        />
      )}
    </View>
  );
  if (!onPress) return content;
  return (
    <Pressable
      style={styles.chipPress}
      onPress={onPress}
      hitSlop={12}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? label}
      accessibilityState={{
        ...(selected !== undefined && { selected }),
        ...(expanded !== undefined && { expanded }),
      }}
    >
      {content}
    </Pressable>
  );
}

/** One pulsing dot for the working bubble — a component so the pulse hook is
 * called once per dot instead of inside a loop (rules-of-hooks). */
function Dot({ palette, delay }: { palette: ChatPalette; delay: number }) {
  const opacity = usePulse(true, 0.3, 500, delay);
  return <Animated.View style={[styles.dot, { backgroundColor: palette.fgMute, opacity }]} />;
}

/** An others-shaped bubble showing three pulsing dots and an optional live caption. */
export function WorkingBubble(props: {
  palette: ChatPalette;
  caption?: string | null;
  accessibilityLabel: string;
  testID?: string;
}) {
  const { palette, caption, accessibilityLabel, testID } = props;
  return (
    <View
      testID={testID}
      accessibilityLabel={accessibilityLabel}
      accessibilityLiveRegion="polite"
      style={[
        styles.bubble,
        {
          backgroundColor: palette.surface,
          borderColor: palette.edge,
          borderWidth: 1,
          alignSelf: "flex-start",
          maxWidth: "94%",
          paddingVertical: 9,
        },
      ]}
    >
      <View style={styles.dots}>
        <Dot palette={palette} delay={0} />
        <Dot palette={palette} delay={160} />
        <Dot palette={palette} delay={320} />
      </View>
      {caption && <Text style={[styles.caption, { color: palette.fgSoft }]}>{caption}</Text>}
    </View>
  );
}

const styles = StyleSheet.create({
  bubble: {
    // React Native defaults flexShrink to 0. Long unbroken content can then use
    // its intrinsic width despite maxWidth and paint outside the bubble.
    flexShrink: 1,
    minWidth: 0,
    borderRadius: BUBBLE_RADIUS,
    paddingVertical: 8,
    paddingHorizontal: 12,
  },
  dimmed: {
    opacity: 0.6,
  },
  meta: {
    flexDirection: "row",
    flexWrap: "wrap",
    alignItems: "center",
    gap: 6,
    marginTop: 3,
    marginHorizontal: 6,
  },
  metaText: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
  },
  chip: {
    flexDirection: "row",
    alignItems: "center",
    // Hugs its label: a chip stretched across the column reads as a bar.
    alignSelf: "flex-start",
    maxWidth: "100%",
    flexShrink: 1,
    gap: 4,
    borderRadius: 10,
    paddingVertical: 3,
    paddingHorizontal: 8,
  },
  chipPress: { alignSelf: "flex-start", maxWidth: "100%", flexShrink: 1 },
  chipLabel: {
    flexShrink: 1,
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
  },
  dots: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
  },
  dot: {
    width: 6,
    height: 6,
    borderRadius: 3,
  },
  caption: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 19,
    marginTop: spacing.xs,
  },
});
