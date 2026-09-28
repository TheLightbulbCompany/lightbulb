import { Host } from "@expo/ui";
import { Picker, Text } from "@expo/ui/swift-ui";
import { pickerStyle, tag } from "@expo/ui/swift-ui/modifiers";
import type { StyleProp, ViewStyle } from "react-native";

/**
 * iOS's own segmented control: SwiftUI's `Picker` in the `.segmented` style
 * (@expo/ui), in place of the hand-drawn chip rows it replaced. iOS draws the
 * sliding thumb, the haptic, the system font and the VoiceOver "1 of 4".
 *
 * Tapping the segment that is already selected does nothing, as on iOS.
 */
export function SegmentedControl<T extends string>({
  label,
  segments,
  value,
  onChange,
  style,
  colorScheme = "light",
}: {
  /** What the control chooses. iOS never draws it; VoiceOver reads it. */
  label: string;
  segments: readonly { key: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
  style?: StyleProp<ViewStyle>;
  /** A nested host does not inherit a surrounding sheet's colour scheme. */
  colorScheme?: "light" | "dark";
}) {
  return (
    // Its own SwiftUI host, so it needs the caller's colour scheme passed in.
    <Host matchContents={{ vertical: true }} colorScheme={colorScheme} style={style}>
      <Picker
        label={label}
        selection={value}
        onSelectionChange={onChange}
        modifiers={[pickerStyle("segmented")]}
      >
        {segments.map((segment) => (
          <Text key={segment.key} modifiers={[tag(segment.key)]}>
            {segment.label}
          </Text>
        ))}
      </Picker>
    </Host>
  );
}
