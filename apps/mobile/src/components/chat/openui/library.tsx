import { createLibrary, defineComponent } from "@openuidev/react-lang";
import { CARD_MAX_OPTIONS, CARD_ROOT, answerIsFormOf, answerIsFrom, answerIsTableOf, choiceReply, formReply, tableReply, type CardName } from "@lightbulb/shared";
import { CARD_COMPONENTS } from "@lightbulb/shared/cards";
import { Image as ExpoImage } from "expo-image";
import { router } from "expo-router";
import { useEffect, useState, type ReactNode } from "react";
import { Linking, Pressable, Text, TextInput, View } from "react-native";
import { WEB_ORIGIN } from "../../../config/domain";
import { fonts, spacing, type ChatPalette } from "../../../config/theme";
import { attachmentFile } from "../../../lib/attachmentFile";
import { activityMobileHref } from "../../../lib/activityLink";
import { sessionMobileHref } from "../../../lib/sessionLink";
import { useCardReply } from "./CardReplyContext";

// Palette and font names are the ones `ChatPalette` / `fonts` in config/theme.ts define.
type RP<P> = { props: P; renderNode: (v: unknown) => ReactNode };
type Sub<P> = { typeName: string; props: P };

// ponytail: one module-level palette set by OpenUICard before render; react-lang renderers take no
// extra props. Upgrade to a React context read inside each renderer if two palettes ever render at once.
let palette: ChatPalette;
export const setCardPalette = (p: ChatPalette) => { palette = p; };

/** What every control reads from the message's press state (the web's CardPress). */
function usePress() {
  const ctx = useCardReply();
  const answered = ctx?.answered ?? null;
  return {
    press: ctx?.press ?? (() => {}),
    disabled: !ctx?.press || !!ctx.pending || answered !== null,
    answered,
    retry: ctx?.retry ?? null,
  };
}

const Chip = ({ label, disabled, pressed, onPress }: { label: string; disabled: boolean; pressed?: boolean; onPress: () => void }) => (
  <Pressable accessibilityRole="button" accessibilityLabel={label} accessibilityState={{ disabled, selected: !!pressed }} disabled={disabled} onPress={onPress}
    style={{ borderWidth: 1, borderColor: pressed ? palette.fg : palette.edge, borderRadius: 999, paddingHorizontal: spacing.md, paddingVertical: spacing.xs, opacity: disabled && !pressed ? 0.6 : 1 }}>
    <Text style={{ fontFamily: fonts.sansMedium, color: palette.fg }}>{label}</Text>
  </Pressable>
);
const Row = ({ children }: { children: ReactNode }) => <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>{children}</View>;
const Prose = ({ children, soft, bold }: { children: ReactNode; soft?: boolean; bold?: boolean }) =>
  <Text style={{ fontFamily: bold ? fonts.sansSemiBold : fonts.sans, color: soft ? palette.fgSoft : palette.fg }}>{children}</Text>;
const Answer = ({ children }: { children: ReactNode }) => <Prose soft>{children}</Prose>;

function SimpleTable({ columns, rows }: { columns: string[]; rows: string[][] }) {
  return (
    <View style={{ borderWidth: 1, borderColor: palette.edge, borderRadius: 6 }}>
      <View style={{ flexDirection: "row", borderBottomWidth: 1, borderColor: palette.edge }}>{columns.map((c, i) => <Text key={i} style={{ flex: 1, padding: spacing.sm, fontFamily: fonts.sansSemiBold, color: palette.fg }}>{c}</Text>)}</View>
      {rows.map((r, i) => <View key={i} style={{ flexDirection: "row" }}>{r.map((cell, j) => <Text key={j} style={{ flex: 1, padding: spacing.sm, color: palette.fg }}>{cell}</Text>)}</View>)}
    </View>
  );
}

const chart = ({ props }: RP<{ labels: string[]; values: number[]; title?: string }>) => (
  <View style={{ gap: spacing.xs }}>
    {props.title ? <Prose soft>{props.title}</Prose> : null}
    <SimpleTable columns={["", "Value"]} rows={props.labels.map((l, i) => [l, String(props.values[i] ?? 0)])} />
  </View>
);

/** Only this message's own image attachment, by file name; a URL in the program never loads. Alt text until it arrives. */
function CardImage({ props }: RP<{ fileName: string; alt: string }>) {
  const ctx = useCardReply();
  const attachment = ctx?.attachments.find((a) => a.fileName === props.fileName && a.contentType.startsWith("image/"));
  const attachmentUrl = ctx?.attachmentUrl;
  const roomId = ctx?.roomId;
  const [loaded, setLoaded] = useState<{ id: string; uri: string } | null>(null);
  useEffect(() => {
    if (!attachment || !attachmentUrl || !roomId) return;
    let live = true;
    void attachmentFile(roomId, attachment, attachmentUrl).then((f) => { if (live) setLoaded({ id: attachment.id, uri: f.uri }); }).catch(() => {});
    return () => { live = false; };
  }, [attachment, attachmentUrl, roomId]);
  if (!attachment || loaded?.id !== attachment.id) return <Prose soft>{props.alt}</Prose>;
  return <ExpoImage source={{ uri: loaded.uri }} accessibilityLabel={props.alt} contentFit="contain" style={{ width: "100%", height: 200 }} />;
}

const input = { borderWidth: 1, borderRadius: 6, padding: spacing.sm } as const;

const R = {
  Stack: ({ props, renderNode }: RP<{ children: unknown }>) => <View style={{ gap: spacing.md }}>{renderNode(props.children)}</View>,
  Text: ({ props }: RP<{ text: string; size?: string }>) => <Prose bold={props.size === "heading"} soft={props.size === "small"}>{props.text}</Prose>,
  Callout: ({ props }: RP<{ text: string; tone?: string }>) => (
    <View style={{ borderLeftWidth: 3, borderLeftColor: props.tone === "warn" ? palette.alert : props.tone === "good" ? palette.live : palette.link, paddingLeft: spacing.sm }}><Prose>{props.text}</Prose></View>
  ),
  Metric: ({ props }: RP<{ label: string; value: string; delta?: string }>) => (
    <View><Prose soft>{props.label}</Prose><Text style={{ fontFamily: fonts.sansSemiBold, fontSize: 22, color: palette.fg }}>{props.value}</Text>{props.delta ? <Prose soft>{props.delta}</Prose> : null}</View>
  ),
  Table: ({ props }: RP<{ columns: Sub<{ label: string; data: (string | number)[] }>[] }>) => {
    const cols = props.columns.map((c) => c.props);
    const n = Math.max(0, ...cols.map((c) => c.data.length));
    return <SimpleTable columns={cols.map((c) => c.label)} rows={Array.from({ length: n }, (_, r) => cols.map((c) => String(c.data[r] ?? "")))} />;
  },
  Col: () => null,
  EditableTable: ({ props }: RP<{ columns: string[]; rows: string[][]; submit?: string }>) => {
    const { press, disabled, answered, retry } = usePress();
    const [rows, setRows] = useState(() => props.rows.map((r) => props.columns.map((_, i) => r[i] ?? "")));
    return <View style={{ gap: spacing.sm }}>
      {rows.map((r, ri) => <View key={ri} style={{ flexDirection: "row", gap: spacing.xs }}>{r.map((cell, ci) =>
        <TextInput key={ci} accessibilityLabel={`${props.columns[ci]}, row ${ri + 1}`} editable={!disabled && retry === null} value={cell}
          onChangeText={(v) => setRows((cur) => cur.map((row, a) => a === ri ? row.map((x, b) => b === ci ? v : x) : row))}
          style={{ ...input, flex: 1, borderColor: palette.edge, color: palette.fg }} />)}</View>)}
      {answerIsTableOf(answered, props.columns) ? <Answer>Sent.</Answer> : <Row><Chip label={props.submit ?? "Submit"} disabled={disabled} onPress={() => press(tableReply(props.columns, rows))} /></Row>}
    </View>;
  },
  BarChart: chart,
  LineChart: chart,
  Steps: ({ props }: RP<{ steps: { label: string; status?: string }[] }>) => (
    <View style={{ gap: spacing.xs }}>{props.steps.map((s, i) => <Prose key={i} soft={s.status === "done"}>{s.status === "done" ? "✓ " : s.status === "doing" ? "› " : "○ "}{s.label}</Prose>)}</View>
  ),
  Sources: ({ props }: RP<{ sources: { title: string; url: string }[] }>) => (
    <View style={{ gap: spacing.xs }}>{props.sources.map((s, i) => {
      // Same order as MessageMarkdown's link rule: a native screen first, then the web, else inert text.
      const native = sessionMobileHref(s.url) ?? activityMobileHref(s.url);
      const web = native ? null : /^https?:\/\//.test(s.url) ? s.url : s.url.startsWith("/") ? `${WEB_ORIGIN}${s.url}` : null;
      return native || web
        ? <Text key={i} accessibilityRole="link" style={{ color: palette.link, textDecorationLine: "underline" }}
            onPress={() => { if (native) router.push(native); else void Linking.openURL(web!); }}>{s.title}</Text>
        : <Prose key={i} soft>{s.title}</Prose>;
    })}</View>
  ),
  Image: CardImage,
  Choice: ({ props }: RP<{ question: string; options: string[]; multi?: boolean }>) => {
    const { press, disabled, answered, retry } = usePress();
    const [picked, setPicked] = useState<string[]>([]);
    const shown = props.options.slice(0, CARD_MAX_OPTIONS);
    const answeredSet = new Set((answered ?? "").split(",").map((s) => s.trim()));
    return <View style={{ gap: spacing.sm }}>
      <Prose bold>{props.question}</Prose>
      {props.multi ? <>
        <Row>{shown.map((o) => <Chip key={o} label={o} disabled={disabled || retry !== null} pressed={answered !== null ? answeredSet.has(o) : picked.includes(o)}
          onPress={() => setPicked((cur) => cur.includes(o) ? cur.filter((p) => p !== o) : [...cur, o])} />)}</Row>
        {answered === null ? <Row><Chip label="Done" disabled={disabled || (retry === null && picked.length === 0)} onPress={() => press(choiceReply(picked))} /></Row> : null}
      </> : <Row>{shown.map((o) => <Chip key={o} label={o} disabled={disabled || (retry !== null && retry !== o.trim())} pressed={answered === o.trim()} onPress={() => press(o)} />)}</Row>}
      {props.options.length > shown.length ? <Prose soft>+{props.options.length - shown.length} more</Prose> : null}
      {answerIsFrom(answered, props.options, props.multi) ? <Answer>You picked {answered}.</Answer> : null}
    </View>;
  },
  Buttons: ({ props }: RP<{ buttons: Sub<{ label: string; variant?: string }>[] }>) => {
    const { press, disabled, answered, retry } = usePress();
    return <View style={{ gap: spacing.sm }}>
      <Row>{props.buttons.map((b, i) => {
        const t = b.props.label.trim();
        return <Chip key={i} label={b.props.label} disabled={disabled || (retry !== null && retry !== t)} pressed={answered === t} onPress={() => press(t)} />;
      })}</Row>
      {answerIsFrom(answered, props.buttons.map((b) => b.props.label)) ? <Answer>You picked {answered}.</Answer> : null}
    </View>;
  },
  Button: () => null,
  FollowUps: ({ props }: RP<{ replies: string[] }>) => {
    const { press, disabled, answered, retry } = usePress();
    return <View style={{ gap: spacing.sm }}>
      <Row>{props.replies.map((r) => <Chip key={r} label={r} disabled={disabled || (retry !== null && retry !== r.trim())} pressed={answered === r.trim()} onPress={() => press(r)} />)}</Row>
      {answerIsFrom(answered, props.replies) ? <Answer>You picked {answered}.</Answer> : null}
    </View>;
  },
  Form: ({ props }: RP<{ title: string; fields: Sub<{ name: string; label: string; placeholder?: string; options?: string[] }>[]; submit?: string }>) => {
    const { press, disabled, answered, retry } = usePress();
    // While an unanswered submit is being retried the fields freeze: the retry replays what was sent, not an edit.
    const frozen = disabled || retry !== null;
    const [values, setValues] = useState<Record<string, string>>({});
    return <View style={{ gap: spacing.sm }}>
      <Prose bold>{props.title}</Prose>
      {props.fields.map((f) => <View key={f.props.name} style={{ gap: spacing.xs }}>
        <Prose soft>{f.props.label}</Prose>
        {f.typeName === "Select"
          ? <Row>{(f.props.options ?? []).map((o) => <Chip key={o} label={o} disabled={frozen} pressed={values[f.props.name] === o} onPress={() => setValues((c) => ({ ...c, [f.props.name]: o }))} />)}</Row>
          : <TextInput accessibilityLabel={f.props.label} editable={!frozen} placeholder={f.typeName === "DatePicker" ? "YYYY-MM-DD" : f.props.placeholder} value={values[f.props.name] ?? ""}
              onChangeText={(v) => setValues((c) => ({ ...c, [f.props.name]: v }))} style={{ ...input, borderColor: palette.edge, color: palette.fg }} />}
      </View>)}
      {answerIsFormOf(answered, props.fields.map((f) => f.props.label)) ? <Answer>Sent.</Answer> : <Row><Chip label={props.submit ?? "Submit"} disabled={disabled} onPress={() => press(formReply(props.fields.map((f) => ({ label: f.props.label, value: values[f.props.name] ?? "" }))))} /></Row>}
    </View>;
  },
  Input: () => null, Select: () => null, DatePicker: () => null,
} satisfies Record<CardName, (p: never) => ReactNode>;

export const mobileCardLibrary = createLibrary({
  root: CARD_ROOT,
  components: CARD_COMPONENTS.map((c) =>
    defineComponent({
      name: c.name,
      description: c.description,
      props: c.props,
      // OpenUI types each renderer by its schema; R is checked by name above.
      component: (R as Record<string, unknown>)[c.name] as never,
    }),
  ),
});
