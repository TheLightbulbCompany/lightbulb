import { SymbolView } from "../ui/SymbolView";
import { StyleSheet, View, type LayoutChangeEvent } from "react-native";

import { radii, useChatPalette } from "../../config/theme";
import { hapticSelection } from "../../lib/haptics";
import { AnimatedPressable, usePressScale } from "../../lib/pressScale";
import { Avatar } from "./Avatar";

/**
 * The conversation header actions — RoomHeader.tsx's own icon
 * buttons, same `aria-label` wording as `accessibilityLabel`. Presentational
 * only: a native header has no room below the buttons for an inline error, so
 * the screen that mounts this owns the async call, the pending flag and the
 * failure copy, and shows it in the screen body instead.
 */
export function RoomHeaderActions({
  starred,
  muted,
  disabled,
  onToggleStar,
  onToggleMute,
  onAddPeople,
  onIdeas,
  voice,
  onLayout,
}: {
  starred: boolean;
  muted: boolean;
  disabled?: boolean;
  onToggleStar: () => void;
  onToggleMute: () => void;
  onAddPeople?: () => void;
  /** Opens the Ideas sheet (the web header's 💡). */
  onIdeas?: () => void;
  /**
   * Go live / Live (any room); passed only when the binary can do voice. It
   * takes the star's place: three controls left a channel name no room, and
   * starring is still on the chat list's row menu.
   */
  voice?: { label: string; active?: boolean; disabled?: boolean; speaking?: { name: string }; onPress: () => void };
  /** The screen bounds its title by this row's width. */
  onLayout?: (event: LayoutChangeEvent) => void;
}) {
  const palette = useChatPalette();
  const starScale = usePressScale();
  const muteScale = usePressScale();
  const voiceScale = usePressScale();
  const addPeopleScale = usePressScale();
  const ideasScale = usePressScale();
  return (
    <View style={styles.row} onLayout={onLayout}>
      {voice ? (
        <AnimatedPressable
          onPress={() => {
            hapticSelection();
            voice.onPress();
          }}
          onPressIn={voiceScale.onPressIn}
          onPressOut={voiceScale.onPressOut}
          disabled={!!voice.disabled}
          hitSlop={8}
          accessibilityRole="button"
          accessibilityLabel={voice.label}
          accessibilityState={{ selected: !!voice.active, disabled: !!voice.disabled }}
          style={[styles.button, { opacity: voice.disabled ? 0.5 : 1 }, voiceScale.style]}
        >
          {voice.speaking ? (
            <Avatar name={voice.speaking.name} kind="agent" size={22} ring="running" palette={palette} />
          ) : (
            <SymbolView
              accessibilityElementsHidden
              name={voice.active ? "phone.fill" : "phone"}
              size={18}
              tintColor={voice.active ? palette.live : palette.fgSoft}
            />
          )}
        </AnimatedPressable>
      ) : (
        <AnimatedPressable
          onPress={() => {
            hapticSelection();
            onToggleStar();
          }}
          onPressIn={starScale.onPressIn}
          onPressOut={starScale.onPressOut}
          disabled={disabled}
          hitSlop={8}
          accessibilityRole="button"
          accessibilityLabel={starred ? "Unstar" : "Star"}
          accessibilityState={{ selected: starred, disabled: !!disabled }}
          // An ARRAY, never a function: reanimated only attaches an animated
          // style it can see in the prop itself, and a function style is opaque
          // to it — the scale would silently never run. The scale is the press
          // feedback; a background tint on top of it would be two signals.
          style={[styles.button, starScale.style]}
        >
          <SymbolView accessibilityElementsHidden name={starred ? "star.fill" : "star"} size={18} tintColor={palette.fgSoft} />
        </AnimatedPressable>
      )}
      {onAddPeople ? (
        <AnimatedPressable
          onPress={() => { hapticSelection(); onAddPeople(); }}
          onPressIn={addPeopleScale.onPressIn}
          onPressOut={addPeopleScale.onPressOut}
          disabled={disabled}
          hitSlop={8}
          accessibilityRole="button"
          accessibilityLabel="Add people"
          accessibilityState={{ disabled: !!disabled }}
          style={[styles.button, addPeopleScale.style]}
        >
          <SymbolView accessibilityElementsHidden name="person.badge.plus" size={18} tintColor={palette.fgSoft} />
        </AnimatedPressable>
      ) : null}
      {onIdeas ? (
        <AnimatedPressable
          onPress={() => { hapticSelection(); onIdeas(); }}
          onPressIn={ideasScale.onPressIn}
          onPressOut={ideasScale.onPressOut}
          hitSlop={8}
          accessibilityRole="button"
          accessibilityLabel="Ideas"
          style={[styles.button, ideasScale.style]}
        >
          <SymbolView accessibilityElementsHidden name="lightbulb" size={18} tintColor={palette.fgSoft} />
        </AnimatedPressable>
      ) : null}
      <AnimatedPressable
        onPress={() => {
          hapticSelection();
          onToggleMute();
        }}
        onPressIn={muteScale.onPressIn}
        onPressOut={muteScale.onPressOut}
        disabled={disabled}
        hitSlop={8}
        accessibilityRole="button"
        accessibilityLabel={muted ? "Unmute" : "Mute"}
        accessibilityState={{ selected: muted, disabled: !!disabled }}
        style={[styles.button, muteScale.style]}
      >
        <SymbolView accessibilityElementsHidden name={muted ? "bell.slash" : "bell"} size={18} tintColor={palette.fgSoft} />
      </AnimatedPressable>
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: "row", alignItems: "center" },
  button: {
    width: 36,
    height: 36,
    borderRadius: radii.sm,
    alignItems: "center",
    justifyContent: "center",
  },
});
