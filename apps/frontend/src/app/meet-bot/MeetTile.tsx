"use client";

import { useEffect } from "react";
import { agentHand, agentLiveStatus, agentStateLine, agentWorking, floorHolder, meetVoiceLimitText, voiceLimit } from "@lightbulb/shared";
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
const MAX_ROWS = 3;
const MAX_ROSTER = 8;
const HINT = "Say an agent's name to ask";
const initialsOf = (name: string) => name.trim().split(/\s+/).slice(0, 2).map((w) => w[0] ?? "").join("").toUpperCase();

function Avatar({ id, name, url, size }: { id: string; name: string; url?: string | null; size: number }) {
  return (
    <span className="inline-flex shrink-0 select-none items-center justify-center overflow-hidden rounded-full font-semibold"
      style={{ width: size, height: size, fontSize: size * 0.4, background: agentTint(id), color: "var(--on-agent-tint)" }}>
      {url ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={url} alt="" className="size-full object-cover" />
      ) : initialsOf(name)}
    </span>
  );
}

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
  const busy = roster.filter((id) => id !== speaker && (isHand(id) || isWorking(id)));
  const activity = [...busy.filter(isHand), ...busy.filter((id) => !isHand(id))];
  const rowLine = (id: string) => isHand(id)
    ? agentStateLine("has_news", { hand: agentHand(attrs, id), meeting: true })
    : agentStateLine("working", { working: agentWorking(attrs, id) });
  const onStage = (id: string) => id === speaker || activity.includes(id);
  const hint = limit
    ? <p className="text-2xl" role="status" style={{ color: "var(--alert)" }}>{meetVoiceLimitText(limit)}</p>
    : <p className="text-2xl" style={{ color: "var(--fg-soft)" }}>{HINT}</p>;
  const stageBusy = !!speaker || activity.length > 0;

  return (
    <div className="relative flex flex-col overflow-hidden" style={{ width: 1280, height: 720, background: "var(--plane-pane)", color: "var(--fg)" }}>
      <style>{`@keyframes meet-ring{0%,100%{box-shadow:0 0 0 6px var(--live)}50%{box-shadow:0 0 0 18px transparent}}
.meet-ring{animation:meet-ring 2s ease-in-out infinite}@media (prefers-reduced-motion:reduce){.meet-ring{animation:none}}`}</style>
      <div className="flex shrink-0 items-center px-10" style={{ height: 64 }}>
        <span className="text-2xl" style={{ color: "var(--fg-mute)" }}>Lightbulb</span>
      </div>
      {notice ? (
        <p className="flex flex-1 items-center justify-center text-5xl" role="status">{notice}</p>
      ) : (
        <>
          <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-8 px-16">
            {speaker && (
              <div className="flex flex-col items-center gap-3">
                <span className="meet-ring rounded-full"><Avatar id={speaker} name={nameOf(speaker)} url={agents[speaker]?.avatarUrl} size={168} /></span>
                <p className="mt-4 text-6xl font-semibold">{nameOf(speaker)}</p>
                <p className="text-3xl" style={{ color: "var(--fg-soft)" }}>{agentStateLine("speaking")}</p>
              </div>
            )}
            {activity.length > 0 && (
              <ul className="flex w-full max-w-4xl flex-col gap-4">
                {activity.slice(0, MAX_ROWS).map((id) => (
                  <li key={id} className="flex items-center gap-5">
                    <Avatar id={id} name={nameOf(id)} url={agents[id]?.avatarUrl} size={48} />
                    <span className="shrink-0 text-3xl font-semibold">{nameOf(id)}</span>
                    <span className="min-w-0 truncate text-3xl" style={{ color: "var(--fg-soft)" }}>{rowLine(id)}</span>
                  </li>
                ))}
                {activity.length > MAX_ROWS && <li className="text-3xl" style={{ color: "var(--fg-soft)" }}>+{activity.length - MAX_ROWS} more</li>}
              </ul>
            )}
            {!stageBusy && (limit
              ? <p className="text-4xl" role="status" style={{ color: "var(--alert)" }}>{meetVoiceLimitText(limit)}</p>
              : <p className="text-4xl" style={{ color: "var(--fg-soft)" }}>{HINT}</p>)}
          </div>
          {roster.length > 0 && (
            <div className="flex shrink-0 flex-col items-center gap-3 pb-6">
              {stageBusy && hint}
              <ul className="flex items-start justify-center gap-6" style={{ height: 84 }}>
                {roster.slice(0, MAX_ROSTER).map((id) => {
                  const live = id === speaker;
                  const on = onStage(id);
                  return (
                    <li key={id} className="flex w-24 flex-col items-center gap-1" style={{ opacity: on ? 1 : 0.55 }}>
                      <span className="rounded-full" style={{ boxShadow: `0 0 0 3px ${live ? "var(--live)" : on ? "var(--plane-edge)" : "transparent"}` }}>
                        <Avatar id={id} name={nameOf(id)} url={agents[id]?.avatarUrl} size={44} />
                      </span>
                      <span className="w-full truncate text-center text-xl" style={{ color: on ? "var(--fg)" : "var(--fg-mute)" }}>{nameOf(id)}</span>
                    </li>
                  );
                })}
                {roster.length > MAX_ROSTER && <li className="self-center text-2xl" style={{ color: "var(--fg-soft)" }}>+{roster.length - MAX_ROSTER}</li>}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  );
}
