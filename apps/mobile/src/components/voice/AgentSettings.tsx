import { createApiClient } from "@lightbulb/shared";
import { useCallback, useMemo, useRef, useState } from "react";
import { useFocusEffect } from "expo-router";
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Switch, Text, TextInput, View } from "react-native";

import { fonts, useChatPalette } from "../../config/theme";
import { useTeamsConfig } from "../../lib/teamsData";

import { PhoneVoiceSettings } from "./PhoneVoiceSettings";

interface AgentDocument {
  frontAgentId?: string | null;
  voice?: string; spanish?: boolean;
  name?: string; greeting?: string; instructions?: string; live?: boolean; paused?: boolean;
  allow?: Record<string, boolean>; handoff?: { number: string; onlyOpenHours: boolean };
  [key: string]: unknown;
}

/** Edit the existing server document whole, retaining settings owned by other surfaces. */
export function AgentSettings() {
  const palette = useChatPalette();
  const adapter = useTeamsConfig();
  const api = useMemo(() => createApiClient(adapter), [adapter]);
  const [agents, setAgents] = useState<{ id: string; name: string }[]>([]);
  const [effectiveFront, setEffectiveFront] = useState<string | null>(null);
  const [frontIssue, setFrontIssue] = useState<string | null>(null);
  const [draft, setDraft] = useState<AgentDocument | null>(null);
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const [message, setMessage] = useState("");
  const [retry, setRetry] = useState(0);
  useFocusEffect(useCallback(() => {
    if (dirtyRef.current) return; // Switching tabs must not silently discard an unsaved edit.
    let alive = true;
    setLoading(true);
    void (api.get("/voice/agent") as Promise<{ config: AgentDocument | null; agents?: { id: string; name: string }[]; frontAgentIssue?: string; frontAgentId?: string }>).then(({ config, agents, frontAgentIssue, frontAgentId }) => {
      if (alive) { setLoaded(true); setEffectiveFront(frontAgentId ?? null); setAgents(agents ?? []); setFrontIssue(frontAgentIssue ?? null); setDraft(config); setDirty(false); setMessage(""); }
    }).catch(() => { if (alive) setMessage("Couldn’t load agent settings. Try again."); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [api, retry]));
  const patch = (next: Partial<AgentDocument>) => { setDraft((current) => ({ ...(current ?? { onboardingStep: 1 }), ...next })); setDirty(true); setMessage(""); };
  const save = async () => {
    if (!draft || saving) return;
    setSaving(true);
    setMessage("");
    try {
      const result = await api.put("/voice/agent", { config: draft }) as { config: AgentDocument };
      setDraft(result.config); setFrontIssue(null); setDirty(false); setMessage("Saved");
    } catch (error) {
      setMessage((error as { status?: number }).status === 403 ? "Only a workspace admin can change agent settings." : "Couldn’t save. Your changes are still here. Try again.");
    } finally { setSaving(false); }
  };
  const text = { color: palette.fg, fontFamily: fonts.sans, fontSize: 16 };
  const field = (label: string, key: "name" | "greeting" | "instructions", multiline = false) => <View style={styles.field}>
    <Text style={[text, styles.label]}>{label}</Text>
    <TextInput accessibilityLabel={label} value={draft?.[key] ?? ""} onChangeText={(value) => patch({ [key]: value })}
      editable={!saving} multiline={multiline} textAlignVertical="top"
      style={[styles.input, text, { borderColor: palette.edge, backgroundColor: palette.surface }, multiline && styles.multiline]} />
  </View>;
  // A receptionist-only save resumes web onboarding without inventing phone settings.
  const phoneConfigured = !!draft && Object.keys(draft).some((key) => key !== "frontAgentId" && key !== "onboardingStep");
  return <ScrollView contentInsetAdjustmentBehavior="automatic" keyboardShouldPersistTaps="handled" keyboardDismissMode="on-drag"
    style={{ backgroundColor: palette.ground }} contentContainerStyle={styles.page}>
    <Text style={[text, styles.title]}>Your assistant, your way</Text>
    <Text style={[text, styles.description, { color: palette.fgSoft }]}>Lightbulb in the app. Choose what callers hear and what your assistant can do.</Text>
    {loading ? <ActivityIndicator accessibilityLabel="Loading agent settings" color={palette.fg} /> : loaded ? <>
      <Text accessibilityRole="header" style={[text, styles.label]}>Front-door agent</Text>
      <Text style={[text, { color: palette.fgSoft }]}>Choose an existing worker to greet people as Lightbulb. Every agent can still do work. New conversations use this choice; existing conversations keep their agent.</Text>
      {frontIssue && <Text accessibilityRole="alert" style={[text, { color: palette.alert }]}>{frontIssue}</Text>}
      {[{ id: "", name: `Workspace default${!draft?.frontAgentId && effectiveFront ? ` · ${agents.find((a) => a.id === effectiveFront)?.name ?? "existing agent"}` : ""}` }, ...agents].map((a) => <Pressable key={a.id}
        accessibilityRole="radio" accessibilityState={{ checked: (draft?.frontAgentId ?? "") === a.id, disabled: saving }}
        accessibilityLabel={`Receptionist: ${a.name}`} disabled={saving} onPress={() => patch({ frontAgentId: a.id || null })}
        style={[styles.input, { borderColor: (draft?.frontAgentId ?? "") === a.id ? palette.fg : palette.edge }]}>
        <Text style={text}>{a.name}</Text>
      </Pressable>)}
      {draft && phoneConfigured ? <>
        <Text style={[text, styles.status, { color: palette.fgSoft }]}>{draft.live ? draft.paused ? "Phone answering is paused" : "Phone answering is live" : "Phone answering is not live"}</Text>
        {field("Name callers hear", "name")}
        {field("Phone greeting", "greeting", true)}
        <PhoneVoiceSettings config={draft} patch={(next) => patch({ ...next })} disabled={saving} />
        {field("Instructions", "instructions", true)}
        <Text accessibilityRole="header" style={[text, styles.label]}>Permissions</Text>
        {([['book', 'Book appointments'], ['move', 'Move appointments'], ['waitlist', 'Add callers to the waitlist'], ['answer', 'Answer business questions'], ['greetByName', 'Greet returning callers by name']] as const).map(([key, label]) =>
          <View key={key} style={[styles.toggle, { borderColor: palette.edge }]}>
            <Text style={[text, styles.toggleLabel]}>{label}</Text>
            <Switch accessibilityLabel={label} disabled={saving} value={draft.allow?.[key] ?? true} onValueChange={(value) => patch({ allow: { ...draft.allow, [key]: value } })} />
          </View>)}
        {draft.live ? <View style={[styles.toggle, { borderColor: palette.edge }]}>
          <Text style={[text, styles.toggleLabel]}>Pause phone answering</Text>
          <Switch accessibilityLabel="Pause phone answering" disabled={saving} value={!!draft.paused} onValueChange={(paused) => patch({ paused })} />
        </View> : null}
      </> : <Text style={[text, { color: palette.fgSoft }]}>Set up your phone assistant on the web to configure its number, hours, and services. You can still talk and type to Lightbulb from Home.</Text>}
      <Pressable accessibilityRole="button" accessibilityLabel="Save agent settings" disabled={!dirty || saving}
        accessibilityState={{ disabled: !dirty || saving, busy: saving }} onPress={() => void save()}
        style={({ pressed }) => [styles.save, { backgroundColor: palette.fg, opacity: !dirty || saving ? 0.45 : pressed ? 0.7 : 1 }]}>
        <Text style={[text, { color: palette.onFg }]}>{saving ? "Saving…" : "Save changes"}</Text>
      </Pressable>
    </> : null}
    {!!message && <Text accessibilityRole={message === "Saved" ? "text" : "alert"} accessibilityLiveRegion="polite" style={[text, { color: message === "Saved" ? palette.live : palette.alert }]}>{message}</Text>}
    {!loading && !draft && !!message && <Pressable accessibilityRole="button" onPress={() => setRetry((n) => n + 1)} style={styles.save}><Text style={[text, { color: palette.link }]}>Retry</Text></Pressable>}
  </ScrollView>;
}

const styles = StyleSheet.create({
  page: { padding: 24, paddingBottom: 48, gap: 20 },
  title: { fontSize: 24, fontFamily: fonts.sansMedium },
  description: { lineHeight: 24 },
  status: { fontSize: 14 },
  field: { gap: 10 },
  label: { fontFamily: fonts.sansMedium },
  input: { minHeight: 48, borderWidth: 1, borderRadius: 12, padding: 12 },
  multiline: { minHeight: 110 },
  toggle: { flexDirection: "row", alignItems: "center", gap: 16, paddingVertical: 8, borderBottomWidth: StyleSheet.hairlineWidth },
  toggleLabel: { flex: 1, lineHeight: 23 },
  save: { minHeight: 48, alignItems: "center", justifyContent: "center", borderRadius: 12, padding: 12 },
});
