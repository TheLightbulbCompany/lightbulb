import { notFound } from "next/navigation";
import { MeetTile, type TileNotice } from "../MeetTile";
import { PreviewScaler } from "./PreviewScaler";

// Dev-only design review: /meet-bot/preview?state=idle|speaking|working|hands|limit|connecting
const NAMES = ["Forge", "Pulse", "Friday", "Gauge", "Atlas", "Tally", "Vera"];
const agents = Object.fromEntries(NAMES.map((n) => [n, { id: n, name: n }]));
const A = (id: string, status: string, extra: Record<string, string> = {}) =>
  Object.fromEntries([[`lightbulb.agent.${id}.status`, status], ...Object.entries(extra).map(([k, v]) => [`lightbulb.agent.${id}.${k}`, v])]);
const idle = (skip: string[] = []) => Object.assign({}, ...NAMES.filter((n) => !skip.includes(n)).map((n) => A(n, "idle")));

const STATES: Record<string, { attrs: Record<string, string>; notice?: TileNotice }> = {
  idle: { attrs: idle() },
  speaking: { attrs: { ...idle(["Forge", "Pulse", "Friday"]), ...A("Forge", "speaking"), "lightbulb.speaking_agent": "Forge",
    ...A("Pulse", "working", { working: "signups and activity this week" }), ...A("Friday", "has_news", { hand: "what is on my calendar today" }) } },
  working: { attrs: { ...idle(["Forge"]), ...A("Forge", "listening", { working: "what pull requests are open right now" }) } },
  hands: { attrs: { ...idle(["Pulse", "Friday", "Gauge", "Atlas"]), ...A("Pulse", "has_news", { hand: "signups are up" }), ...A("Friday", "has_news", { hand: "a meeting moved" }),
    ...A("Gauge", "has_news", { hand: "churn looks odd" }), ...A("Atlas", "working", { working: "the Q3 plan" }) } },
  limit: { attrs: { ...idle(), "lightbulb.voice_limit": "free_minutes_used" } },
  connecting: { attrs: {}, notice: "Connecting…" },
};

export default async function Page({ searchParams }: { searchParams: Promise<{ state?: string }> }) {
  if (process.env.NODE_ENV === "production") notFound();
  const { state = "idle" } = await searchParams;
  const s = STATES[state] ?? STATES.idle;
  const tile = <MeetTile agents={agents} attrs={s.attrs} notice={s.notice ?? null} />;
  return (
    <div style={{ background: "var(--plane-rail)", minHeight: "100vh", padding: 16, display: "flex", gap: 24, alignItems: "flex-start" }}>
      <PreviewScaler width={1280} height={720} fit>{tile}</PreviewScaler>
      <PreviewScaler width={1280} height={720} scale={0.25}>{tile}</PreviewScaler>
    </div>
  );
}
