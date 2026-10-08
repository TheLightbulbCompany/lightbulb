import { ImageResponse } from "next/og";
import { GLASS_PATH } from "@/components/Logo";
import { SHARE_CARD as C } from "@/components/landing/shareCard";

// One card for every session link: the link's token is in the URL fragment, which no server (and
// so no unfurler) ever sees, so the preview cannot name a session or its owner.
export const alt = "A live coding session shared on Lightbulb";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function SessionLinkCard() {
  return new ImageResponse(
    <div style={{ width: "100%", height: "100%", display: "flex", flexDirection: "column", justifyContent: "space-between", padding: 64, background: C.bg, color: C.fg }}>
      <div style={{ display: "flex", flexDirection: "column", flex: 1, borderRadius: 20, border: `2px solid ${C.edge}` }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "20px 24px", borderBottom: `2px solid ${C.edge}` }}>
          {[0, 1, 2].map((dot) => <div key={dot} style={{ width: 16, height: 16, borderRadius: 8, background: C.edge }} />)}
        </div>
        <div style={{ display: "flex", flexDirection: "column", flex: 1, justifyContent: "center", gap: 20, padding: "0 56px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 20, fontSize: 84, fontWeight: 700 }}>
            <div style={{ width: 28, height: 28, borderRadius: 14, background: C.live }} />
            Live session
          </div>
          <div style={{ display: "flex", fontSize: 34, color: C.mute }}>Open to watch this coding session live in a terminal.</div>
        </div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 14, marginTop: 36, fontSize: 34, letterSpacing: 6 }}>
        <svg width="26" height="50" viewBox="62 40 76 148">
          <path d={GLASS_PATH} fill={C.bulb} />
          <rect x="86" y="150" width="28" height="7" fill={C.fg} />
          <rect x="87" y="160" width="26" height="7" fill={C.fg} />
          <rect x="89" y="170" width="22" height="6" fill={C.fg} />
        </svg>
        LIGHTBULB
      </div>
    </div>,
    size,
  );
}
