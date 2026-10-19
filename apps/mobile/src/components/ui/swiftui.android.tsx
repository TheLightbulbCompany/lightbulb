import { Children, isValidElement, type ReactElement, type ReactNode, useState } from "react";
import { Pressable, Text as RNText, View, type TextStyle, type ViewStyle } from "react-native";

import { fonts } from "../../config/theme";
import { NavigationRow, useGroupedPalette } from "./GroupedList";
import { InsetSectionHeader, insetRowStyle } from "./InsetGroup";
import { SymbolView } from "./SymbolView";

/**
 * Android has no SwiftUI. These are React Native stand-ins for the handful of
 * @expo/ui/swift-ui views the grouped-list screens (Settings, room details,
 * the workspace gate) use, with the same props, drawn as the app's own inset
 * cards. Modifiers are read for colour, text style and frame; the rest are
 * iOS-only and ignored.
 */
type Modifier = { $type: string; [key: string]: unknown };
type WithModifiers = { modifiers?: unknown[] };

const TEXT_SIZE: Record<string, number> = {
  largeTitle: 34,
  title: 28,
  title2: 22,
  title3: 20,
  headline: 17,
  body: 17,
  callout: 16,
  subheadline: 15,
  footnote: 13,
  caption: 12,
  caption2: 11,
};

function modifierStyle(modifiers: unknown[] | undefined): TextStyle {
  const style: TextStyle = {};
  for (const raw of modifiers ?? []) {
    const m = raw as Modifier;
    if (m.$type === "foregroundStyle" || m.$type === "foregroundColor") {
      const c = (m as { style?: unknown; color?: unknown }).style ?? m.color;
      if (typeof c === "string") style.color = c;
    } else if (m.$type === "font") {
      const size = TEXT_SIZE[m.textStyle as string] ?? (m.size as number | undefined);
      if (size) style.fontSize = size;
      if (m.weight === "semibold" || m.weight === "bold") style.fontFamily = fonts.sansSemiBold;
    } else if (m.$type === "frame") {
      if (typeof m.width === "number") style.width = m.width;
      if (typeof m.height === "number") style.height = m.height;
    } else if (m.$type === "background" && typeof m.color === "string") {
      style.backgroundColor = m.color;
      if (typeof m.cornerRadius === "number") style.borderRadius = m.cornerRadius;
      style.textAlign = "center";
      style.textAlignVertical = "center";
      style.overflow = "hidden";
    }
  }
  return style;
}

export function Text({ children, modifiers }: WithModifiers & { children?: ReactNode }) {
  const p = useGroupedPalette();
  return (
    <RNText style={[{ fontFamily: fonts.sans, fontSize: 17, color: p.fg }, modifierStyle(modifiers)]}>
      {children}
    </RNText>
  );
}

export function HStack({ children, spacing = 8 }: { children?: ReactNode; spacing?: number }) {
  return <View style={{ flexDirection: "row", alignItems: "center", gap: spacing }}>{children}</View>;
}

export function VStack({ children, spacing = 0 }: { children?: ReactNode; spacing?: number; alignment?: string }) {
  return <View style={{ flexShrink: 1, gap: spacing }}>{children}</View>;
}

const ROW: ViewStyle = { minHeight: 48, paddingHorizontal: 16, paddingVertical: 12, justifyContent: "center" };

export function Section({
  title,
  footer,
  children,
}: {
  title?: string;
  footer?: ReactNode;
  children?: ReactNode;
}) {
  const p = useGroupedPalette();
  const rows = Children.toArray(children);
  return (
    <View>
      {title ? <InsetSectionHeader title={title.toUpperCase()} palette={p} /> : <View style={{ height: 16 }} />}
      {rows.map((row, i) => (
        <View
          key={isValidElement(row) && row.key != null ? row.key : i}
          style={[
            insetRowStyle(p, i, rows.length),
            i > 0 ? { borderTopWidth: 1, borderTopColor: p.edge } : null,
          ]}
        >
          <RowFrame row={row} />
        </View>
      ))}
      {footer ? <View style={{ paddingHorizontal: 32, paddingBottom: 8 }}>{footer}</View> : null}
    </View>
  );
}

/** A bare Text (or anything that is not itself a row) gets the row's padding. */
function RowFrame({ row }: { row: ReactNode }) {
  const pressable = isValidElement(row) && (row.type === Button || row.type === Picker || row.type === NavigationRow);
  return pressable ? <>{row}</> : <View style={ROW}>{row}</View>;
}

export function Button({
  label,
  onPress,
  modifiers,
  children,
}: WithModifiers & { label?: string; onPress?: () => void; role?: string; children?: ReactNode }) {
  const p = useGroupedPalette();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      onPress={onPress}
      style={({ pressed }) => [ROW, pressed ? { backgroundColor: p.selected } : null]}
    >
      {children ?? (
        <RNText style={[{ fontFamily: fonts.sans, fontSize: 17, color: p.link }, modifierStyle(modifiers)]}>
          {label}
        </RNText>
      )}
    </Pressable>
  );
}

export function LabeledContent({ label, children }: { label: ReactNode; children?: ReactNode }) {
  return (
    <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
      <View style={{ flexShrink: 1 }}>{label}</View>
      <View style={{ flexShrink: 1, alignItems: "flex-end" }}>{children}</View>
    </View>
  );
}

/** A menu picker as a row that expands to its options, the checked one ticked. */
export function Picker<T extends string>({
  label,
  selection,
  onSelectionChange,
  children,
}: WithModifiers & {
  label?: string;
  selection: T;
  onSelectionChange: (value: T) => void;
  children?: ReactNode;
}) {
  const p = useGroupedPalette();
  const [open, setOpen] = useState(false);
  const options = Children.toArray(children)
    .filter(isValidElement)
    .map((el) => {
      const element = el as ReactElement<WithModifiers & { children?: ReactNode }>;
      const tagged = (element.props.modifiers ?? []).find((m) => !!m && typeof m === "object" && "tag" in m) as
        | { tag: T }
        | undefined;
      return { value: tagged?.tag as T, label: element.props.children };
    });
  const current = options.find((o) => o.value === selection);
  return (
    <View>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={label}
        accessibilityState={{ expanded: open }}
        onPress={() => setOpen((v) => !v)}
        style={[ROW, { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 12 }]}
      >
        <RNText style={{ fontFamily: fonts.sans, fontSize: 17, color: p.fg, flexShrink: 1 }}>{label}</RNText>
        <RNText style={{ fontFamily: fonts.sans, fontSize: 17, color: p.fgMute }}>{current?.label}</RNText>
      </Pressable>
      {open
        ? options.map((o) => (
            <Pressable
              key={o.value}
              accessibilityRole="radio"
              accessibilityState={{ checked: o.value === selection }}
              onPress={() => {
                setOpen(false);
                if (o.value !== selection) onSelectionChange(o.value);
              }}
              style={[ROW, { flexDirection: "row", alignItems: "center", gap: 8, paddingLeft: 32 }]}
            >
              <RNText style={{ fontFamily: fonts.sans, fontSize: 16, color: p.fg, flex: 1 }}>{o.label}</RNText>
              {o.value === selection ? <SymbolView name="checkmark" size={18} tintColor={p.link} /> : null}
            </Pressable>
          ))
        : null}
    </View>
  );
}
