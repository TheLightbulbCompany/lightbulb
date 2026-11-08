"use client";

import { useEffect } from "react";
import { Hand } from "lucide-react";
import { agentLiveStatus, agentStateLine, agentWorking, floorHolder, meetVoiceLimitText, voiceLimit } from "@lightbulb/shared";
import { agentTint } from "@/lib/agentTint";

// The palette tokens live on <html>; stamp the dark theme there and put the previous value back on unmount.
export function useDarkTheme() {
  useEffect(() => {
    const el = document.documentElement;
    const prev = el.getAttribute("data-chat-theme");
    el.setAttribute("data-chat-theme", "dark");
    return () => { if (prev === null) el.removeAttribute("data-chat-theme"); else el.setAttribute("data-chat-theme", prev); };
  }, []);
}

// The bot's camera video: a fixed 1280x720 canvas that Meet shows as a small tile. Nobody can click it.
export type TileAgent = { id: string; name: string; avatarUrl?: string | null };
export type TileNotice = "Connecting…" | "Reconnecting…" | "This link has expired." | "Couldn't reach the agents." | null;

const STATUS_RE = /^lightbulb\.agent\.(.+)\.status$/;
const HINT = "Say an agent's name to ask";
// The share of the canvas that holds content. Seen in a live Meet: a wide tile cut 5% off the top and bottom,
// a tall one 3% off each side; a 4:3 grid tile cuts 12% off each side.
const SAFE = 0.8;
const cap = (w: string) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
// "Forge" -> "Fo"; "Gauge Prime" -> "GP", so two agents with the same first letter still differ.
export const initialsOf = (name: string) => {
  const w = name.trim().split(/\s+/).filter(Boolean);
  return w.length > 1 ? (w[0][0] + w[1][0]).toUpperCase() : cap(w[0] ?? "").slice(0, 2);
};

function Avatar({ id, name, url, size }: { id: string; name: string; url?: string | null; size: number }) {
  return (
    <span className="inline-flex shrink-0 select-none items-center justify-center overflow-hidden rounded-full font-semibold"
      style={{ width: size, height: size, fontSize: size * 0.38, background: agentTint(id), color: "var(--on-agent-tint)" }}>
      {url ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={url} alt="" className="size-full object-cover" />
      ) : initialsOf(name)}
    </span>
  );
}

const soft = { color: "var(--fg-soft)" } as const;

export function MeetTile({ agents, attrs, notice }: { agents: Record<string, TileAgent>; attrs: Record<string, string>; notice: TileNotice }) {
  const nameOf = (id: string) => agents[id]?.name ?? (attrs[`lightbulb.agent.${id}.name`] || id);
  const ids = Object.keys(attrs).map((k) => STATUS_RE.exec(k)?.[1]).filter((id): id is string => !!id);
  // Fixed order: name (case-insensitive), then id. Never depends on who is active.
  const roster = [...new Set(ids)].sort((a, b) => nameOf(a).toLowerCase().localeCompare(nameOf(b).toLowerCase()) || a.localeCompare(b));
  const limit = voiceLimit(attrs);
  const floor = floorHolder(attrs);
  const speaking = roster.filter((id) => agentLiveStatus(attrs, id) === "speaking");
  const speaker = speaking.find((id) => !floor || floor === id) ?? speaking[0];
  // The agent that holds the floor reads `listening` while its run works; the task text says it is busy.
  const isWorking = (id: string) => {
    const s = agentLiveStatus(attrs, id);
    return s === "working" || ((s === "listening" || s === "joining") && !!agentWorking(attrs, id));
  };
  const isHand = (id: string) => agentLiveStatus(attrs, id) === "has_news";
  // Hand first: with nobody speaking, the first raised hand is the hero. A Recall bot cannot raise Meet's own
  // hand (no API; our own guest is refused, 10-06), so the tile is the hand. No topic and no instruction
  // (owner ruling 10-06): it speaks in the next gap by itself.
  const raised = speaker ? undefined : roster.find(isHand);
  const busy = roster.filter((id) => id !== speaker && id !== raised && (isHand(id) || isWorking(id)));
  const activity = [...busy.filter(isHand), ...busy.filter((id) => !isHand(id))];
  const rowLine = (id: string) => isHand(id)
    ? "Hand up"
    // no request text (owner ruling 10-06): "Working", or "Listening to <name>" for a name it just heard
    : agentWorking(attrs, id).startsWith("listening to ") ? agentStateLine("working", { working: agentWorking(attrs, id) }) : "Working";
  const onStage = (id: string) => id === speaker || id === raised || activity.includes(id);
  const stageBusy = !!speaker || !!raised || activity.length > 0;
  const hero = speaker ?? raised;
  const maxRows = hero ? 2 : 3;
  const rowSize = hero ? { av: 52, name: 34, line: 34, gap: 12 } : { av: 80, name: 46, line: 38, gap: 20 };
  const limitText = limit ? meetVoiceLimitText(limit) : "";
  // The page can be in the room before the worker is: until an agent shows up, nobody is listening yet.
  const shown = notice ?? (roster.length === 0 && !limit ? "Connecting…" : null);
  // Even rows, six across at most: seven agents sit 4 + 3, never 5 + 2.
  const heroCount = Math.min(roster.length, 10) + (roster.length > 10 ? 1 : 0);
  const heroCols = Math.ceil(heroCount / Math.ceil(heroCount / 6)) || 1;
  const idleCaption = limit
    ? <p className="max-w-[980px] text-balance text-center" role="status" style={{ fontSize: 40, lineHeight: 1.2, color: "var(--alert)" }}>{limitText}</p>
    : <p className="text-center" style={{ fontSize: 36, lineHeight: 1.2, ...soft }}>{HINT}</p>;
  const stripHint = limit
    ? <p className="max-w-[980px] text-balance text-center" role="status" style={{ fontSize: 28, lineHeight: 1.2, color: "var(--alert)" }}>{limitText}</p>
    : <p className="text-center" style={{ fontSize: 24, lineHeight: "32px", ...soft }}>{HINT}</p>;

  return (
    <div className="relative overflow-hidden" style={{ width: 1280, height: 720, background: "var(--plane-pane)", color: "var(--fg)" }}>
      {/* Meet crops the camera to its tile's shape (the sides in a tall tile, top and bottom in a wide one):
          everything is laid out on the full canvas, then drawn at SAFE of it in the middle, so no label or
          name sits in the part a tile cuts off. The background still fills the frame. */}
      <div data-safe className="absolute inset-0 flex flex-col" style={{ transform: `scale(${SAFE})` }}>
      <style>{`.meet-ring{box-shadow:0 0 0 6px var(--plane-pane),0 0 0 12px var(--live)}
@keyframes meet-pulse{0%,100%{box-shadow:0 0 0 6px var(--plane-pane),0 0 0 12px var(--live),0 0 0 12px color-mix(in srgb,var(--live) 35%,transparent)}
50%{box-shadow:0 0 0 6px var(--plane-pane),0 0 0 12px var(--live),0 0 0 26px color-mix(in srgb,var(--live) 0%,transparent)}}
.meet-ring{animation:meet-pulse 2.2s ease-in-out infinite}
@keyframes meet-hand{0%,100%{transform:translateY(0) rotate(0)}30%{transform:translateY(-10px) rotate(-8deg)}60%{transform:translateY(-4px) rotate(6deg)}}
.meet-hand{animation:meet-hand 1.8s ease-in-out infinite;transform-origin:50% 90%}
@media (prefers-reduced-motion:reduce){.meet-ring,.meet-hand{animation:none}}`}</style>
      <span className="absolute left-10 top-5 text-2xl" style={{ color: "var(--fg-mute)" }}>Lightbulb</span>
      {shown ? (
        <p className="flex flex-1 items-center justify-center text-5xl" role="status">{shown}</p>
      ) : !stageBusy ? (
        // Idle: the agents themselves are the hero; no bottom strip repeats them.
        <div className="flex min-h-0 flex-1 flex-col items-center justify-center px-16 py-8" style={{ gap: 32 }}>
          {roster.length > 0 && (
            <ul data-roster className="flex flex-wrap justify-center" style={{ gap: 32, columnGap: 40, width: heroCols * 150 + (heroCols - 1) * 40 }}>
              {roster.slice(0, 10).map((id) => (
                <li key={id} className="flex flex-col items-center" style={{ width: 150, gap: 8 }}>
                  <Avatar id={id} name={nameOf(id)} url={agents[id]?.avatarUrl} size={96} />
                  <span data-name className="w-full truncate text-center" style={{ fontSize: 28, lineHeight: "34px" }}>{nameOf(id)}</span>
                </li>
              ))}
              {roster.length > 10 && <li className="self-center" style={{ fontSize: 28, ...soft }}>+{roster.length - 10}</li>}
            </ul>
          )}
          {idleCaption}
        </div>
      ) : (
        <>
          <div className="flex min-h-0 flex-1 flex-col items-center justify-center px-16 py-8" style={{ gap: 16 }}>
            {speaker && (
              <div className="flex flex-col items-center">
                <span className="meet-ring rounded-full"><Avatar id={speaker} name={nameOf(speaker)} url={agents[speaker]?.avatarUrl} size={200} /></span>
                <p data-speaker-name className="max-w-[1100px] truncate font-semibold" style={{ fontSize: 72, lineHeight: "76px", marginTop: 20 }}>{nameOf(speaker)}</p>
                <p style={{ fontSize: 34, lineHeight: "40px", ...soft }}>{agentStateLine("speaking")}</p>
              </div>
            )}
            {raised && (
              <div data-hand className="flex flex-col items-center">
                <span className="relative rounded-full" style={{ boxShadow: "0 0 0 6px var(--plane-pane), 0 0 0 12px var(--link)" }}>
                  <Avatar id={raised} name={nameOf(raised)} url={agents[raised]?.avatarUrl} size={180} />
                  <span className="absolute flex items-center justify-center rounded-full"
                    style={{ right: -22, bottom: -10, width: 92, height: 92, background: "var(--link)", color: "var(--plane-pane)", boxShadow: "0 0 0 6px var(--plane-pane)" }}>
                    <Hand className="meet-hand" size={54} strokeWidth={2.2} aria-hidden />
                  </span>
                </span>
                <p data-speaker-name className="max-w-[1100px] truncate font-semibold" style={{ fontSize: 64, lineHeight: "70px", marginTop: 22 }}>{nameOf(raised)} has an answer</p>
              </div>
            )}
            {activity.length > 0 && (
              <div className="mx-auto w-fit max-w-[1000px]">
                <ul className="grid items-center" style={{ gridTemplateColumns: "auto auto minmax(0, 1fr)", columnGap: 24, rowGap: rowSize.gap }}>
                  {activity.slice(0, maxRows).map((id) => (
                    <li key={id} className="contents">
                      <Avatar id={id} name={nameOf(id)} url={agents[id]?.avatarUrl} size={rowSize.av} />
                      <span data-row-name className="max-w-[420px] truncate font-semibold" style={{ fontSize: rowSize.name, lineHeight: 1.1 }}>{nameOf(id)}</span>
                      <span className="min-w-0 truncate" style={{ fontSize: rowSize.line, lineHeight: 1.2, ...soft }}>{rowLine(id)}</span>
                    </li>
                  ))}
                </ul>
                {activity.length > maxRows && <p style={{ fontSize: 32, lineHeight: 1.2, marginTop: rowSize.gap, ...soft }}>+{activity.length - maxRows} more</p>}
              </div>
            )}
          </div>
          <div className="flex shrink-0 flex-col items-center pb-4" style={{ gap: 8 }}>
            {stripHint}
            <ul data-roster className="flex items-start justify-center" style={{ gap: 24 }}>
              {roster.slice(0, 8).map((id) => {
                const live = id === speaker;
                const on = onStage(id);
                return (
                  <li key={id} className="flex flex-col items-center" style={{ width: 96, gap: 4, opacity: on ? 1 : 0.55 }}>
                    <span className="rounded-full" style={{ boxShadow: `0 0 0 3px ${live ? "var(--live)" : on ? "var(--plane-edge)" : "transparent"}` }}>
                      <Avatar id={id} name={nameOf(id)} url={agents[id]?.avatarUrl} size={52} />
                    </span>
                    <span data-name className="w-full truncate text-center" style={{ fontSize: 22, lineHeight: "26px", color: on ? "var(--fg)" : "var(--fg-mute)" }}>{nameOf(id)}</span>
                  </li>
                );
              })}
              {roster.length > 8 && <li className="self-center" style={{ fontSize: 24, ...soft }}>+{roster.length - 8}</li>}
            </ul>
          </div>
        </>
      )}
      </div>
    </div>
  );
}
