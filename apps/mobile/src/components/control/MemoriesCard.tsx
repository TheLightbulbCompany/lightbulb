import { useAuth } from "@clerk/expo";
import { useCallback, useEffect, useState } from "react";
import { Alert, Pressable, StyleSheet, Text, View } from "react-native";
import type { createApiClient } from "@lightbulb/shared";

import { colors, fonts, radii, spacing } from "../../config/theme";

type ApiClient = ReturnType<typeof createApiClient>;
interface Memory { id: string; text: string; createdAt: string; confirmed: boolean }
interface MemoryList { private: Memory[]; team: Memory[]; truncated: boolean }

// A new record is listed by GET /memory only ~60 s after it is written, so a share/confirm/delete
// is held in this module-level overlay (survives unmount) and merged into every fetched list.
// Each entry is scoped to "org:user" so switching workspace never shows A's rows in B.
// Mirrors the web AgentMemoryTab; mobile does not share it (web code is not importable here).
const HOLD_MS = 90_000;
const added = new Map<string, { row: Memory; until: number; scope: string }>();
const removed = new Map<string, { until: number; scope: string }>();

/** TEST ONLY: clear the overlay between tests. */
export function resetMemoryOverlay() {
  added.clear();
  removed.clear();
}

function overlay(d: MemoryList, scope: string): MemoryList {
  const now = Date.now();
  for (const [k, v] of added) if (v.until < now) added.delete(k);
  for (const [k, v] of removed) if (v.until < now) removed.delete(k);
  const mine = (v: { scope: string } | undefined) => v?.scope === scope;
  const keep = (m: Memory) => !mine(removed.get(m.id)) && !mine(added.get(m.id));
  const rows = [...added.values()].filter(mine).map((v) => v.row);
  return { ...d, private: d.private.filter(keep), team: [...rows, ...d.team.filter(keep)] };
}

/** What this workspace's agents remember. Memory is per workspace; Share and Confirm are backend acts. */
export function MemoriesCard({ api }: { api: ApiClient }) {
  const { orgId, userId } = useAuth();
  const scope = `${orgId ?? ""}:${userId ?? ""}`;
  const [raw, setRaw] = useState<MemoryList | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [, bump] = useState(0);

  const load = useCallback(async () => {
    try {
      setRaw((await api.get("/memory")) as MemoryList);
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, [api]);
  useEffect(() => void load(), [load]);

  async function act(id: string, run: () => Promise<unknown>, apply: (res: { id?: string }) => void) {
    setBusy((b) => new Set(b).add(id));
    try {
      apply((await run()) as { id?: string });
      bump((n) => n + 1);
    } catch (e) {
      Alert.alert("Couldn’t update memory", e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setBusy((b) => {
        const n = new Set(b);
        n.delete(id);
        return n;
      });
    }
  }

  // Put a confirmed team row in the overlay; the old record (if its id changed) stays hidden.
  function hold(old: Memory, newId: string | undefined) {
    const until = Date.now() + HOLD_MS;
    const id = newId ?? old.id;
    if (id !== old.id) removed.set(old.id, { until, scope });
    added.set(id, { row: { ...old, id, confirmed: true }, until, scope });
  }

  function button(label: string, m: Memory, onPress: () => void, danger = false) {
    return (
      <Pressable accessibilityRole="button" accessibilityLabel={label} disabled={busy.has(m.id)} onPress={onPress}
        style={busy.has(m.id) ? styles.dim : undefined}>
        <Text style={[styles.action, danger && styles.danger]}>{label}</Text>
      </Pressable>
    );
  }

  function row(m: Memory, kind: "private" | "team") {
    return (
      <View key={m.id} style={styles.row}>
        <Text style={styles.text}>{m.text}</Text>
        <View style={styles.actions}>
          {m.confirmed ? <Text style={styles.confirmed}>Confirmed</Text> : null}
          {kind === "private"
            ? button("Share with team", m, () => void act(m.id, () => api.post(`/memory/${m.id}/share`, {}), (r) => hold(m, r.id)))
            : null}
          {kind === "team" && !m.confirmed
            ? button("Confirm", m, () => void act(m.id, () => api.post(`/memory/${m.id}/confirm`, {}), (r) => hold(m, r.id)))
            : null}
          {button("Delete", m, () =>
            Alert.alert("Delete this memory?", "Agents will no longer recall it.", [
              { text: "Cancel", style: "cancel" },
              {
                text: "Delete",
                style: "destructive",
                onPress: () =>
                  void act(m.id, () => api.del(`/memory/${m.id}`), () => {
                    removed.set(m.id, { until: Date.now() + HOLD_MS, scope });
                    added.delete(m.id);
                  }),
              },
            ]), true)}
        </View>
      </View>
    );
  }

  function section(title: string, note: string, rows: Memory[], kind: "private" | "team") {
    return (
      <View>
        <Text style={styles.heading}>{title}</Text>
        <Text style={styles.note}>{note}</Text>
        {rows.length ? rows.map((m) => row(m, kind)) : <Text style={styles.note}>Nothing remembered yet.</Text>}
      </View>
    );
  }

  if (failed) return <View style={styles.card}><Text style={styles.danger}>Memory is unavailable right now.</Text></View>;
  if (!raw) return <View style={styles.card}><Text style={styles.note}>Loading memory…</Text></View>;
  const data = overlay(raw, scope);
  return (
    <View style={styles.card}>
      <Text style={styles.note}>Shared by every agent in this workspace.</Text>
      {section("ONLY YOU", "From your direct messages with agents.", data.private, "private")}
      {section("TEAM", "Every member and every agent can recall these.", data.team, "team")}
      {data.truncated ? <Text style={styles.note}>Showing the newest 200.</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  card: { borderRadius: radii.lg, borderWidth: 1, borderColor: colors.edge, padding: spacing.lg, gap: spacing.sm },
  heading: { fontFamily: fonts.sansMedium, fontSize: 10, letterSpacing: 1.9, color: colors.mute, marginTop: spacing.md },
  note: { fontFamily: fonts.sans, fontSize: 12, color: colors.mute },
  row: { borderBottomWidth: 1, borderBottomColor: colors.edge, paddingVertical: spacing.sm, gap: spacing.xs },
  text: { fontFamily: fonts.sans, fontSize: 15, color: colors.ink },
  actions: { flexDirection: "row", alignItems: "center", gap: spacing.lg },
  action: { fontFamily: fonts.sansMedium, fontSize: 13, color: colors.info },
  danger: { fontFamily: fonts.sansMedium, fontSize: 13, color: colors.danger },
  confirmed: { fontFamily: fonts.sans, fontSize: 12, color: colors.mute },
  dim: { opacity: 0.5 },
});
