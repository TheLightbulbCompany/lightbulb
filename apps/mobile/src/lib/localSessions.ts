import type { LocalSession, RoomDetail, RoomResources } from "@lightbulb/shared";

type RoomMember = RoomDetail["members"][number];

/** Sessions a reader would want to glance at now: working, or blocked on the owner. */
export const liveSessionCount = (sessions: readonly LocalSession[] | undefined): number =>
  (sessions ?? []).filter((s) => s.status === "busy" || s.status === "waiting-approval").length;

/** A room member's display name by human id; the web's fallback copy when unknown. */
export const memberName = (members: readonly RoomMember[] | undefined, humanUserId: string): string =>
  members?.find((m) => m.kind === "human" && m.id === humanUserId)?.displayName ?? "Room member";

export type DeviceRow =
  | { kind: "mac"; key: string; deviceId: string; pairedLabel: string | null; sessions: LocalSession[] }
  | { kind: "cli"; key: string; deviceId: string };
export interface DeviceSection { key: string; title: string; data: DeviceRow[] }

/**
 * The web RoomResources grouping: one section per human, a row per Mac (with
 * its sessions) and one per CLI endpoint that has no Mac association.
 */
export function deviceSections(data: RoomResources, name: (humanUserId: string) => string): DeviceSection[] {
  const humans = new Set([
    ...data.devices.map((d) => d.humanUserId),
    ...data.pairedNodes.map((n) => n.humanUserId),
    ...data.sessions.map((s) => s.humanUserId),
  ]);
  return [...humans].map((human) => {
    const macs = new Set([
      ...data.devices.filter((d) => d.humanUserId === human).map((d) => d.deviceId),
      ...data.pairedNodes.filter((n) => n.humanUserId === human && n.clientId === "openclaw-macos").map((n) => n.deviceId),
      ...data.sessions.filter((s) => s.humanUserId === human).map((s) => s.deviceId),
    ]);
    const rows: DeviceRow[] = [...macs].map((mac) => {
      const nodes = data.pairedNodes.filter((n) => n.humanUserId === human && n.macDeviceId === mac);
      return {
        kind: "mac",
        key: `mac:${mac}`,
        deviceId: mac,
        pairedLabel: nodes.length > 0
          ? `Paired ${nodes.some((n) => n.clientId === "node-host") ? "Mac + associated CLI" : "Mac"} · availability unknown`
          : null,
        sessions: data.sessions.filter((s) => s.humanUserId === human && s.deviceId === mac),
      };
    });
    for (const node of data.pairedNodes.filter((n) => n.humanUserId === human && n.clientId === "node-host" && !n.macDeviceId)) {
      rows.push({ kind: "cli", key: `cli:${node.deviceId}`, deviceId: node.deviceId });
    }
    return { key: human, title: name(human), data: rows };
  });
}
