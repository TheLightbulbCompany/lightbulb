import { SymbolView as NativeSymbolView, type AndroidSymbol, type SymbolViewProps } from "expo-symbols";

export type { SFSymbol } from "expo-symbols";

/**
 * Android has no SF Symbols: expo-symbols renders only `fallback` for a plain
 * SF name, so every icon (send, close, chevrons) vanished. Map the SF names
 * this app uses onto Material Symbols, which expo-symbols draws on Android.
 * An unmapped name drops its `.fill`-style suffixes and tries again, then
 * renders the caller's fallback, as before.
 */
const MATERIAL: Record<string, AndroidSymbol> = {
  archivebox: "archive",
  "arrow.down": "arrow_downward",
  "arrow.left.arrow.right": "swap_horiz",
  "arrow.triangle.branch": "fork_right",
  "arrow.triangle.pull": "merge",
  "arrow.up": "arrow_upward",
  "arrowshape.turn.up.left": "reply",
  at: "alternate_email",
  bag: "shopping_bag",
  bell: "notifications",
  "bell.slash": "notifications_off",
  bold: "format_bold",
  bolt: "bolt",
  "bubble.left": "chat_bubble",
  "bubble.left.and.bubble.right": "forum",
  "building.2": "apartment",
  "chart.bar": "bar_chart",
  folder: "folder",
  checkmark: "check",
  "checkmark.circle": "check_circle",
  "checkmark.square": "check_box",
  "chevron.down": "expand_more",
  "chevron.left": "chevron_left",
  "chevron.left.forwardslash.chevron.right": "code",
  "chevron.right": "chevron_right",
  "chevron.up": "expand_less",
  cpu: "memory",
  creditcard: "credit_card",
  cube: "deployed_code",
  curlybraces: "data_object",
  doc: "description",
  "doc.on.doc": "content_copy",
  "doc.text": "description",
  document: "description",
  ellipsis: "more_horiz",
  envelope: "mail",
  "envelope.badge": "mark_email_unread",
  "envelope.open": "drafts",
  "exclamationmark.circle": "error",
  "exclamationmark.triangle": "warning",
  eye: "visibility",
  "face.smiling": "mood",
  flag: "flag",
  gearshape: "settings",
  globe: "language",
  "hand.raised": "front_hand",
  italic: "format_italic",
  lightbulb: "lightbulb",
  link: "link",
  "list.bullet": "format_list_bulleted",
  "list.number": "format_list_numbered",
  lock: "lock",
  magnifyingglass: "search",
  message: "chat_bubble",
  mic: "mic",
  "mic.slash": "mic_off",
  minus: "remove",
  "network.slash": "cloud_off",
  number: "tag",
  paperclip: "attach_file",
  pencil: "edit",
  person: "person",
  "person.2": "group",
  "person.badge.plus": "person_add",
  phone: "call",
  "phone.down": "call_end",
  photo: "image",
  "play.rectangle": "smart_display",
  plus: "add",
  "puzzlepiece.extension": "extension",
  "questionmark.circle": "help",
  repeat: "repeat",
  "server.rack": "dns",
  shield: "shield",
  shippingbox: "package_2",
  "slider.horizontal.3": "tune",
  "smallcircle.filled.circle": "radio_button_checked",
  sparkles: "auto_awesome",
  "square.and.arrow.up": "ios_share",
  "square.and.pencil": "edit_square",
  "square.grid.2x2": "grid_view",
  star: "star",
  "star.slash": "star_half",
  stop: "stop",
  strikethrough: "format_strikethrough",
  terminal: "terminal",
  "text.bubble": "chat",
  textformat: "text_format",
  trash: "delete",
  underline: "format_underlined",
  "waveform.path.ecg": "monitor_heart",
  "wrench.and.screwdriver": "build",
  xmark: "close",
  "xmark.circle": "cancel",
};

const SUFFIXES = /\.(fill|circle|square|badge)$/;

export function materialFor(sf: string): AndroidSymbol | undefined {
  let name = sf;
  while (true) {
    const hit = MATERIAL[name];
    if (hit) return hit;
    const shorter = name.replace(SUFFIXES, "");
    if (shorter === name) return undefined;
    name = shorter;
  }
}

export function SymbolView(props: SymbolViewProps) {
  if (typeof props.name !== "string") return <NativeSymbolView {...props} />;
  const android = materialFor(props.name);
  return <NativeSymbolView {...props} name={android ? { android } : props.name} />;
}
