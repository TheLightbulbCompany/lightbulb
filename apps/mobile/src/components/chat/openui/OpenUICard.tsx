import { Renderer, useRenderNode, type ElementNode } from "@openuidev/react-lang";
import { CARD_MAX_BYTES } from "@lightbulb/shared";
import { useState } from "react";
import { Text, View } from "react-native";
import { fonts, spacing, type ChatPalette } from "../../../config/theme";
import { useCardReply } from "./CardReplyContext";
import { mobileCardLibrary, setCardPalette } from "./library";

/** One ```openui-lang fence from an agent's reply. Controls read the message's press state (CardReplyContext). */
export function OpenUICard({ program, palette }: { program: string; palette: ChatPalette }) {
  const ctx = useCardReply();
  const [root, setRoot] = useState<ElementNode | null>(null);
  const [unparsed, setUnparsed] = useState(false);
  setCardPalette(palette);
  if (new TextEncoder().encode(program).length > CARD_MAX_BYTES) return <Text selectable style={{ color: palette.fg }}>{program}</Text>;
  return (
    <View style={{ marginTop: spacing.sm, borderWidth: 1, borderColor: palette.edge, borderRadius: 10, backgroundColor: palette.raised, padding: spacing.md, gap: spacing.sm }}>
      <Renderer response={program} library={mobileCardLibrary} isStreaming={false} onParseResult={(r) => { setRoot(r?.root ?? null); setUnparsed(r !== null && r.root === null); }}>
        <Root node={root} />
      </Renderer>
      {unparsed ? <Text selectable style={{ fontFamily: fonts.mono, color: palette.fg }}>{program}</Text> : null}
      {ctx?.error ? <Text accessibilityRole="alert" style={{ fontFamily: fonts.sans, color: palette.alert }}>{ctx.error}</Text> : null}
    </View>
  );
}

/**
 * The Renderer's default content slot wraps the tree in a DOM <div>, which React Native cannot mount
 * (it took the whole room screen down on device, 2026-10-17; jest's host renderer accepts any tag, so
 * the suite never saw it). This slot renders the parsed root bare, through the Renderer's own runtime.
 */
function Root({ node }: { node: ElementNode | null }) {
  const renderNode = useRenderNode();
  return node ? <>{renderNode(node)}</> : null;
}
