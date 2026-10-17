import { Renderer } from "@openuidev/react-lang";
import { CARD_MAX_BYTES } from "@lightbulb/shared";
import { Text, View } from "react-native";
import { fonts, spacing, type ChatPalette } from "../../../config/theme";
import { useCardReply } from "./CardReplyContext";
import { mobileCardLibrary, setCardPalette } from "./library";

/** One ```openui-lang fence from an agent's reply. Controls read the message's press state (CardReplyContext). */
export function OpenUICard({ program, palette }: { program: string; palette: ChatPalette }) {
  const ctx = useCardReply();
  setCardPalette(palette);
  if (new TextEncoder().encode(program).length > CARD_MAX_BYTES) return <Text selectable style={{ color: palette.fg }}>{program}</Text>;
  return (
    <View style={{ marginTop: spacing.sm, borderWidth: 1, borderColor: palette.edge, borderRadius: 10, backgroundColor: palette.raised, padding: spacing.md, gap: spacing.sm }}>
      <Renderer response={program} library={mobileCardLibrary} isStreaming={false} />
      {ctx?.error ? <Text accessibilityRole="alert" style={{ fontFamily: fonts.sans, color: palette.alert }}>{ctx.error}</Text> : null}
    </View>
  );
}
