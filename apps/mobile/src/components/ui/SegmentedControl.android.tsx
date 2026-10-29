import { Pressable, Text, View, type StyleProp, type ViewStyle } from "react-native";

import { chatPalette, fonts } from "../../config/theme";

/**
 * Android's segmented control: a row of pressable segments (iOS draws
 * SwiftUI's segmented `Picker`; see SegmentedControl.tsx). Same props.
 */
export function SegmentedControl<T extends string>({
  label,
  segments,
  value,
  onChange,
  style,
  colorScheme = "light",
}: {
  label: string;
  segments: readonly { key: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
  style?: StyleProp<ViewStyle>;
  colorScheme?: "light" | "dark";
}) {
  const p = chatPalette[colorScheme];
  return (
    <View
      accessibilityRole="tablist"
      accessibilityLabel={label}
      style={[{ flexDirection: "row", padding: 2, borderRadius: 9, backgroundColor: p.chip }, style]}
    >
      {segments.map((segment) => {
        const selected = segment.key === value;
        return (
          <Pressable
            key={segment.key}
            accessibilityRole="tab"
            accessibilityState={{ selected }}
            onPress={() => {
              if (!selected) onChange(segment.key);
            }}
            style={{
              flex: 1,
              alignItems: "center",
              paddingVertical: 6,
              borderRadius: 7,
              backgroundColor: selected ? p.surface : undefined,
            }}
          >
            <Text style={{ fontFamily: selected ? fonts.sansSemiBold : fonts.sans, fontSize: 14, color: p.fg }}>
              {segment.label}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}
