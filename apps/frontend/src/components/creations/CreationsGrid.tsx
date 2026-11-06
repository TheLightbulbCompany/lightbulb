"use client";

// Creations (super-agent, UX slice 5): everything Lightbulb and the specialists made, in one grid. No text box and no
// primary button: you make things by asking, so "Make something new" drafts an ask in the Lightbulb DM and sends
// nothing. Replaces Library and Canvas on the rail; Canvas's ?app= deep link still opens one app (CanvasDockPanel).

import "./creations.css";
import { useMemo, useState, type ReactElement } from "react";
import { AppWindow, FileText, Mail, PanelsTopLeft } from "lucide-react";
import { elapsedShort, roomDisplayName, specialistLabel, type CreationOutput } from "@lightbulb/shared";
import { useMessagingWorkspaceOptional } from "@/components/messaging/MessagingWorkspaceContext";
import { projectRooms } from "@/components/home/HomeProjects";
import { openChatPanelWithDraft } from "@/components/workspace/workspaceController";
import { creationState } from "@/components/canvas/appText";
import { useApps, type AppRow } from "@/hooks/useApps";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { KIND_FILTERS, KIND_WORD, openOutput } from "./openOutput";
import { OutputThumb } from "./OutputThumb";
import { useCreations } from "./useCreations";

/** Live app pages framed on the grid; every app after these (Load more included) shows its glyph. */
const MAX_FRAMES = 6;

/** The cursor lands on the first "…", selected, so typing replaces it. */
const MAKE = [
  { label: "App", noun: "an app", hint: "A small app", Icon: AppWindow, draft: "Build me a small app that …" },
  { label: "Doc", noun: "a doc", hint: "A brief, a plan", Icon: FileText, draft: "Write a doc: …" },
  { label: "Email", noun: "an email", hint: "A draft to send", Icon: Mail, draft: "Draft an email to … about …" },
  { label: "Page", noun: "a page", hint: "A one-page site", Icon: PanelsTopLeft, draft: "Make a one-page site for …" },
] as const;

export function CreationsGrid(): ReactElement {
  const [kind, setKind] = useState("all");
  const [roomId, setRoomId] = useState("");
  const kinds = KIND_FILTERS.find((f) => f.id === kind)?.kinds;
  const filter = useMemo(() => ({ ...(kinds ? { kind: kinds } : {}), ...(roomId ? { roomId } : {}) }), [kinds, roomId]);
  const { outputs, loading, error, retry, hasMore, loadingMore, loadMore } = useCreations(filter);
  const { apps } = useApps();
  const { agentNames } = useLiveEvents();
  const rooms = useMessagingWorkspaceOptional()?.directoryProps?.rooms;
  const projects = useMemo(() => projectRooms(rooms ?? []), [rooms]);
  const projectName = (id: string | null) => {
    const room = id ? rooms?.find((r) => r.roomId === id) : undefined;
    return room ? roomDisplayName(room, room.participants) : null;
  };
  const filtered = kind !== "all" || !!roomId;
  const [noAgent, setNoAgent] = useState(false);
  // The first MAX_FRAMES apps that would frame (public, openable) get a live page; the rest their glyph.
  const framed = new Set<number>();
  outputs.forEach((o, i) => {
    const app = o.kind === "app" ? apps.find((a) => a.app_id === o.ref?.appId) : undefined;
    if (framed.size < MAX_FRAMES && app?.open_to === "public" && app.can_open !== false) framed.add(i);
  });
  const [now] = useState(() => Date.now()); // ages as of the page's opening

  return <div className="creations"><div className="frame-doc creations__col">
    <div className="frame-doc__title"><h1>Creations</h1></div>
    <section aria-labelledby="creations-make" className="creations__make">
      <h2 id="creations-make" className="creations__label">Make something new</h2>
      <div className="creations__tiles">
        {MAKE.map(({ label, noun, hint, Icon, draft }) => <button key={label} type="button" className="creations__tile"
          aria-label={`Make ${noun}: drafts the ask to Lightbulb, sends nothing`}
          onClick={() => setNoAgent(!openChatPanelWithDraft(draft))}>
          <Icon size={18} strokeWidth={1.5} aria-hidden="true" />
          <span className="creations__tile-name">{label}</span>
          <span className="creations__tile-hint">{hint}</span>
        </button>)}
      </div>
      {noAgent && <p role="status" className="creations__note">Lightbulb isn&apos;t ready yet. Try again in a moment.</p>}
    </section>

    <div className="creations__bar">
      <div className="creations__seg" role="group" aria-label="Show">
        {KIND_FILTERS.map((f) => <button key={f.id} type="button" aria-pressed={kind === f.id} onClick={() => setKind(f.id)}>{f.label}</button>)}
      </div>
      <select className="creations__select" aria-label="Project" value={roomId} onChange={(e) => setRoomId(e.target.value)}>
        <option value="">All projects</option>
        {projects.map((r) => <option key={r.roomId} value={r.roomId}>{roomDisplayName(r, r.participants)}</option>)}
      </select>
    </div>

    {error ? <p role="alert" className="creations__note">Creations couldn&apos;t load.{" "}
      <button type="button" className="creations__link" onClick={retry}>Retry</button></p>
      : loading ? <ul className="creations__grid" aria-busy="true" aria-label="Loading creations">
        {Array.from({ length: 6 }, (_, i) => <li key={i} className="creations__card" aria-hidden="true"><span className="output-thumb" data-size="tile" /></li>)}
      </ul>
      : outputs.length === 0 ? <p className="creations__note">{filtered ? "Nothing here yet." : "Everything Lightbulb and the specialists make lands here."}</p>
      : <ul className="creations__grid" aria-label="Creations">
        {outputs.map((o, i) => <CreationCard key={`${o.taskId}:${o.id}`} output={o} now={now} frame={framed.has(i)}
          app={o.kind === "app" ? apps.find((a) => a.app_id === o.ref?.appId) : undefined}
          project={projectName(o.roomId)} who={specialistLabel(o.specialist, agentNames)} />)}
      </ul>}
    {hasMore && !loading && !error && <div className="creations__more">
      <button type="button" className="creations__more-btn" disabled={loadingMore} onClick={loadMore}>{loadingMore ? "Loading…" : "Load more"}</button>
    </div>}
  </div></div>;
}

function CreationCard({ output: o, app, project, who, now, frame }: {
  output: CreationOutput; app?: AppRow; project: string | null; who: string; now: number; frame: boolean;
}): ReactElement {
  const state = app ? creationState(app, who === "Lightbulb" ? "Engineering" : who, now) : null;
  const meta = [KIND_WORD[o.kind], project, elapsedShort(o.createdAt, now)].filter(Boolean).join(" · ");
  return <li className="creations__card">
    <button type="button" className="creations__open" aria-label={`Open ${o.title}`} onClick={() => openOutput(o)}>
      <OutputThumb output={o} app={app} frame={frame} />
    </button>
    <p className="creations__title" title={o.title}>{o.title}</p>
    <p className="creations__meta">{meta}</p>
    {state && <p className="creations__state">{state}</p>}
  </li>;
}
