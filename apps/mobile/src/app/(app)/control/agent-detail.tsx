import { useAuth } from "@clerk/expo";
import { SymbolView } from "../../../components/ui/SymbolView";
import { router, Stack, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  ActivityIndicator,
  Alert,
  Animated,
  FlatList,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  useColorScheme,
  View,
} from "react-native";
import {
  agentDisplayName,
  createApiClient,
  getAgentModelString,
  useAgents,
  useStatusLoaded,
  useRuntimeRpc,
  useRuntimeRpcMutation,
  type Agent,
} from "@lightbulb/shared";

import { AgentMonogram } from "../../../components/control/AgentMonogram";
import { MemoriesCard } from "../../../components/control/MemoriesCard";
import { usePulse } from "../../../components/chat/usePulse";
import { chatPalette, colors, fonts, radii, spacing, type ChatPalette } from "../../../config/theme";
import {
  buildModelNameMap,
  buildSelectorModels,
  displayModelName,
  type CatalogModel,
} from "../../../lib/agentCatalog";
import { makeAdapter } from "../../../lib/adapter";
import { useLiveEvents } from "../../../providers/live-events";

const VOICE_NOTES_MAX = 1000;
interface VoiceNotes { name: string; notes: string; voice: string }

// ---------------------------------------------------------------------------
// config.get snapshot — the model catalog + per-agent config entry (tools /
// workspace / paperclip) that agents.list does not carry. Untyped runtime
// data from the container, so every access below is defensive.
// ---------------------------------------------------------------------------

interface ConfigAgentEntry {
  id?: string;
  model?: unknown;
  workspace?: string;
  tools?: { profile?: string; alsoAllow?: string[]; deny?: string[] };
  params?: { paperclip?: { role?: string; title?: string } };
}

/** config.get returns `{path, exists, raw, config, hash?, valid}` — the real
 *  config object is under `.config` (fall back to the snapshot itself for
 *  deployments that inline it). `models.list` (AgentCore) has no envelope, so
 *  its `{models: [...], defaultId}` payload falls through unchanged. */
function extractConfig(snapshot: unknown): unknown {
  if (snapshot && typeof snapshot === "object" && "config" in snapshot) {
    const inner = (snapshot as { config?: unknown }).config;
    if (inner && typeof inner === "object") return inner;
  }
  return snapshot;
}

function findConfigEntry(config: unknown, agentId: string): ConfigAgentEntry | undefined {
  const list = (config as { agents?: { list?: unknown } } | null | undefined)
    ?.agents?.list;
  if (!Array.isArray(list)) return undefined;
  return (list as ConfigAgentEntry[]).find((e) => e?.id === agentId);
}

// ---------------------------------------------------------------------------
// Model picker (spec §4 sheet). Lists the gateway catalog; a "Use default"
// row clears the override (agents.update {model:null}). When the catalog is
// empty (BYO / no providers block) it degrades to a free-text ref field —
// noted inline, v1.
// ---------------------------------------------------------------------------

// There is no "Use default" row and no free-text ref box: each agent's own
// profile IS the setting, so `{model: null}` would write nothing, and a ref the
// backend never offered is refused (M6 task 10-C, row 31).
function ModelPickerModal({
  visible,
  models,
  currentRef,
  onClose,
  onSelect,
}: {
  visible: boolean;
  models: CatalogModel[];
  currentRef: string | undefined;
  onClose: () => void;
  onSelect: (ref: string | null) => void;
}) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.modalRoot}>
        <Pressable style={styles.scrim} onPress={onClose} />
        <View style={[styles.sheet, styles.pickerSheet]}>
          <View style={styles.sheetHeader}>
            <Text style={styles.sheetTitle}>Model</Text>
            <Pressable onPress={onClose} hitSlop={8} accessibilityLabel="Close">
              <SymbolView accessibilityElementsHidden name="xmark" size={18} tintColor={palette.fgMute} />
            </Pressable>
          </View>

          <FlatList
            data={models}
            keyExtractor={(m) => m.ref}
            style={styles.pickerList}
            renderItem={({ item }) => {
              const selected = item.ref === currentRef;
              return (
                <Pressable
                  style={({ pressed }) => [
                    styles.pickerRow,
                    pressed && styles.pickerRowPressed,
                  ]}
                  onPress={() => onSelect(item.ref)}
                >
                  <View style={styles.pickerRowText}>
                    <Text style={styles.pickerName} numberOfLines={1}>
                      {item.name}
                    </Text>
                    <Text style={styles.pickerRef} numberOfLines={1}>
                      {item.ref}
                    </Text>
                  </View>
                  {selected && (
                    <SymbolView accessibilityElementsHidden name="checkmark" size={18} tintColor={palette.fg} />
                  )}
                </Pressable>
              );
            }}
          />
        </View>
      </View>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Building blocks
// ---------------------------------------------------------------------------

function InfoRow({
  label,
  value,
  onPress,
  disabled,
}: {
  label: string;
  value: ReactNode;
  onPress?: () => void;
  disabled?: boolean;
}) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const content = (
    <>
      <Text style={styles.rowLabel}>{label}</Text>
      <View style={styles.rowValueWrap}>
        {typeof value === "string" ? (
          <Text style={styles.rowValue} numberOfLines={2}>
            {value}
          </Text>
        ) : (
          value
        )}
        {onPress && !disabled && (
          <SymbolView accessibilityElementsHidden name="chevron.right" size={16} tintColor={palette.fgMute} />
        )}
      </View>
    </>
  );
  if (onPress && !disabled) {
    return (
      <Pressable
        style={({ pressed }) => [styles.infoRow, pressed && styles.rowPressed]}
        onPress={onPress}
      >
        {content}
      </Pressable>
    );
  }
  return <View style={styles.infoRow}>{content}</View>;
}

function CardSkeleton() {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const pulse = usePulse(true, 0.55);
  return (
    <View style={styles.card}>
      {[0, 1, 2].map((i) => (
        <Animated.View key={i} style={[styles.skeletonLine, { opacity: pulse }]} />
      ))}
    </View>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function AgentDetail() {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const { agentId, name: paramName } = useLocalSearchParams<{
    agentId: string;
    name?: string;
  }>();
  const live = useLiveEvents();
  const callRpc = useRuntimeRpcMutation(live);

  const { agents: roster, defaultId, refresh: mutateAgents } = useAgents(live);
  // The model list comes from `models.list` — the owner's own Bedrock inference
  // profiles, or Hermes's catalog via the bridge. Held until a status body names
  // this workspace's runtime: firing on a guess costs a ~55s microVM cold start.
  const statusLoaded = useStatusLoaded();
  const { data: configSnapshot, mutate: mutateConfig } = useRuntimeRpc<unknown>(
    live,
    statusLoaded ? "models.list" : null,
  );

  const agent: Agent | undefined = useMemo(
    () => roster.find((a) => a.id === agentId),
    [roster, agentId],
  );
  const isDefault = agentId != null && agentId === defaultId;

  const config = useMemo(() => extractConfig(configSnapshot), [configSnapshot]);
  const configEntry = useMemo(
    () => (agentId ? findConfigEntry(config, agentId) : undefined),
    [config, agentId],
  );

  const displayName = agent ? agentDisplayName(agent) : paramName || agentId || "Agent";
  const currentModelRef = getAgentModelString(agent);
  const modelNames = useMemo(() => buildModelNameMap(config), [config]);
  const modelDisplay = currentModelRef
    ? displayModelName(currentModelRef, modelNames) || currentModelRef
    : null;
  const selectorModels = useMemo(
    () => buildSelectorModels(config, currentModelRef),
    [config, currentModelRef],
  );

  // ---- Name editing ----
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [savingName, setSavingName] = useState(false);

  const startEditName = useCallback(() => {
    setNameDraft(displayName);
    setEditingName(true);
  }, [displayName]);

  const saveName = useCallback(async () => {
    const next = nameDraft.trim();
    if (!agentId || !next || next === displayName) {
      setEditingName(false);
      return;
    }
    setSavingName(true);
    try {
      await callRpc("agents.update", { agentId, name: next });
      await mutateAgents();
      setEditingName(false);
    } catch (err) {
      Alert.alert(
        "Couldn’t rename agent",
        err instanceof Error ? err.message : String(err),
      );
    } finally {
      setSavingName(false);
    }
  }, [agentId, nameDraft, displayName, callRpc, mutateAgents]);

  // ---- Model editing ----
  const [pickerOpen, setPickerOpen] = useState(false);
  const [savingModel, setSavingModel] = useState(false);

  const selectModel = useCallback(
    async (ref: string | null) => {
      setPickerOpen(false);
      if (!agentId) return;
      setSavingModel(true);
      try {
        // {model: null} clears the override → inherit the gateway default.
        await callRpc("agents.update", { agentId, model: ref });
        await Promise.all([mutateAgents(), mutateConfig()]);
      } catch (err) {
        Alert.alert(
          "Couldn’t update model",
          err instanceof Error ? err.message : String(err),
        );
      } finally {
        setSavingModel(false);
      }
    },
    [agentId, callRpc, mutateAgents, mutateConfig],
  );

  // ---- Delete ----
  const confirmDelete = useCallback(() => {
    if (!agent) return;
    Alert.alert(
      `Delete ${displayName}?`,
      "This removes the agent and its workspace files. This cannot be undone.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Delete",
          style: "destructive",
          onPress: () => {
            void callRpc("agents.delete", { agentId: agent.id, deleteFiles: true })
              .then(() => mutateAgents())
              .then(() => router.back())
              .catch((err: unknown) => {
                Alert.alert(
                  "Couldn’t delete agent",
                  err instanceof Error ? err.message : String(err),
                );
              });
          },
        },
      ],
    );
  }, [agent, displayName, callRpc, mutateAgents]);

  // ---- Voice notes (C9). Loads on its own, never through the runtime RPC. ----
  const { getToken } = useAuth();
  const tokenRef = useRef(getToken);
  tokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => tokenRef.current())), []);
  const [voice, setVoice] = useState<VoiceNotes | null>(null);
  const [voiceFailed, setVoiceFailed] = useState(false);
  const [notesOpen, setNotesOpen] = useState(false);
  const [notesDraft, setNotesDraft] = useState("");
  const [savingNotes, setSavingNotes] = useState(false);
  const notesPath = agentId ? `/teams/agents/${encodeURIComponent(agentId)}/voice-notes` : null;

  const loadVoice = useCallback(async () => {
    if (!notesPath) return;
    setVoice((await api.get(notesPath)) as VoiceNotes);
  }, [api, notesPath]);
  useEffect(() => {
    loadVoice().catch(() => setVoiceFailed(true));
  }, [loadVoice]);

  const saveNotes = useCallback(async () => {
    if (!notesPath) return;
    setSavingNotes(true);
    try {
      await api.put(notesPath, { notes: notesDraft });
      await loadVoice();
      setNotesOpen(false);
    } catch (err) {
      Alert.alert("Couldn’t save voice notes", err instanceof Error ? err.message : String(err));
    } finally {
      setSavingNotes(false);
    }
  }, [api, notesPath, notesDraft, loadVoice]);

  const paperclip = configEntry?.params?.paperclip;
  const tools = configEntry?.tools;
  const workspace = configEntry?.workspace;
  const configLoading = configSnapshot === undefined;

  return (
    <ScrollView
      contentInsetAdjustmentBehavior="automatic"
      automaticallyAdjustKeyboardInsets
      style={styles.page}
      contentContainerStyle={styles.content}
      keyboardShouldPersistTaps="handled"
    >
      <Stack.Screen options={{ title: displayName }} />

      {/* Identity header */}
      <View style={styles.header}>
        <AgentMonogram
          name={displayName}
          emoji={agent?.identity?.emoji ?? undefined}
          size={44}
        />
        <View style={styles.headerText}>
          <View style={styles.headerNameRow}>
            <Text style={styles.headerName} numberOfLines={1}>
              {displayName}
            </Text>
            {isDefault && <Text style={styles.defaultTag}>DEFAULT</Text>}
          </View>
          <Text style={styles.headerId} numberOfLines={1}>
            {agentId}
          </Text>
        </View>
      </View>

      {/* Settings card */}
      <Text style={styles.sectionLabel}>SETTINGS</Text>
      <View style={styles.card}>
        {/* Name (editable) */}
        {editingName ? (
          <View style={styles.editRow}>
            <TextInput
              style={styles.input}
              value={nameDraft}
              onChangeText={setNameDraft}
              autoFocus
              autoCapitalize="words"
              editable={!savingName}
              returnKeyType="done"
              onSubmitEditing={() => void saveName()}
            />
            <View style={styles.editActions}>
              <Pressable
                style={({ pressed }) => [styles.ghostButton, pressed && styles.pressed]}
                onPress={() => setEditingName(false)}
                disabled={savingName}
              >
                <Text style={styles.ghostButtonText}>Cancel</Text>
              </Pressable>
              <Pressable
                style={({ pressed }) => [
                  styles.primaryButton,
                  savingName && styles.primaryButtonDisabled,
                  pressed && styles.pressed,
                ]}
                onPress={() => void saveName()}
                disabled={savingName}
              >
                {savingName ? (
                  <ActivityIndicator size="small" color={palette.onFg} />
                ) : (
                  <Text style={styles.primaryButtonText}>Save</Text>
                )}
              </Pressable>
            </View>
          </View>
        ) : (
          <InfoRow label="Name" value={displayName} onPress={startEditName} />
        )}
        <View style={styles.divider} />
        {/* Model (picker) */}
        <InfoRow
          label="Model"
          value={
            savingModel ? (
              <ActivityIndicator size="small" color={palette.fgMute} />
            ) : (
              <Text style={styles.rowValue} numberOfLines={2}>
                {modelDisplay ?? "Uses default model"}
              </Text>
            )
          }
          onPress={() => setPickerOpen(true)}
          disabled={savingModel}
        />
        {workspace ? (
          <>
            <View style={styles.divider} />
            <InfoRow label="Workspace" value={workspace} />
          </>
        ) : null}
      </View>

      {/* Paperclip role (read-only), when present */}
      {paperclip?.role || paperclip?.title ? (
        <>
          <Text style={styles.sectionLabel}>ROLE</Text>
          <View style={styles.card}>
            {paperclip?.role ? (
              <InfoRow label="Role" value={paperclip.role} />
            ) : null}
            {paperclip?.title ? (
              <>
                {paperclip?.role ? <View style={styles.divider} /> : null}
                <InfoRow label="Title" value={paperclip.title} />
              </>
            ) : null}
          </View>
        </>
      ) : null}

      <Text style={styles.sectionLabel}>MEMORY</Text>
      <MemoriesCard api={api} />

      {/* Voice */}
      <Text style={styles.sectionLabel}>VOICE</Text>
      <View style={styles.card}>
        <InfoRow label="Voice" value={voice ? voice.voice.charAt(0).toUpperCase() + voice.voice.slice(1) : "…"} />
        <View style={styles.divider} />
        {/* The editor opens only on loaded notes: an empty draft saved over a failed load would wipe them. */}
        <InfoRow
          label="Voice notes"
          value={voice ? voice.notes.split("\n")[0] || "None" : voiceFailed ? "Unavailable" : "…"}
          onPress={voice ? () => { setNotesDraft(voice.notes); setNotesOpen(true); } : undefined}
        />
      </View>

      {/* Tools (read-only) */}
      <Text style={styles.sectionLabel}>TOOLS</Text>
      {configLoading ? (
        <CardSkeleton />
      ) : (
        <View style={styles.card}>
          {tools ? (
            <View style={styles.toolsBody}>
              <InfoRow label="Profile" value={tools.profile ?? "default"} />
              {tools.alsoAllow && tools.alsoAllow.length > 0 ? (
                <>
                  <View style={styles.divider} />
                  <InfoRow label="Also allow" value={tools.alsoAllow.join(", ")} />
                </>
              ) : null}
              {tools.deny && tools.deny.length > 0 ? (
                <>
                  <View style={styles.divider} />
                  <InfoRow label="Deny" value={tools.deny.join(", ")} />
                </>
              ) : null}
            </View>
          ) : (
            <View style={styles.infoRow}>
              <Text style={styles.note}>Uses the default tool set.</Text>
            </View>
          )}
        </View>
      )}
      <Text style={styles.footnote}>
        Tool editing lives in the web control panel on thelightbulb.company.
      </Text>

      {/* Danger zone */}
      {agent ? (
        <Pressable
          style={({ pressed }) => [styles.deleteButton, pressed && styles.pressed]}
          onPress={confirmDelete}
          accessibilityRole="button"
          accessibilityLabel="Delete agent"
        >
          <SymbolView accessibilityElementsHidden name="trash" size={16} tintColor={palette.alert} />
          <Text style={styles.deleteText}>Delete agent</Text>
        </Pressable>
      ) : null}

      <Modal visible={notesOpen} animationType="slide" presentationStyle="pageSheet" onRequestClose={() => setNotesOpen(false)}>
        <View style={[styles.sheet, { flex: 1 }]}>
          <Text style={styles.sheetTitle}>Voice notes</Text>
          <TextInput
            style={[styles.input, { minHeight: 140, textAlignVertical: "top" }]}
            value={notesDraft}
            onChangeText={setNotesDraft}
            maxLength={VOICE_NOTES_MAX}
            multiline
            editable={!savingNotes}
            accessibilityLabel="Voice notes"
          />
          <Text style={styles.footnote} accessibilityLiveRegion="polite">
            {notesDraft.length} / 1,000
          </Text>
          <Text style={styles.footnote}>
            How {displayName} should talk and what to remember when speaking in Live. Kept in the agent’s profile.
          </Text>
          <View style={styles.sheetActions}>
            <Pressable style={({ pressed }) => [styles.ghostButton, pressed && styles.pressed]} onPress={() => setNotesOpen(false)} disabled={savingNotes}>
              <Text style={styles.ghostButtonText}>Cancel</Text>
            </Pressable>
            <Pressable
              style={({ pressed }) => [styles.primaryButton, savingNotes && styles.primaryButtonDisabled, pressed && styles.pressed]}
              onPress={() => void saveNotes()}
              disabled={savingNotes}
            >
              {savingNotes ? <ActivityIndicator size="small" color={palette.onFg} /> : <Text style={styles.primaryButtonText}>Save</Text>}
            </Pressable>
          </View>
        </View>
      </Modal>
      <ModelPickerModal
        visible={pickerOpen}
        models={selectorModels}
        currentRef={currentModelRef}
        onClose={() => setPickerOpen(false)}
        onSelect={(ref) => void selectModel(ref)}
      />
    </ScrollView>
  );
}

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    page: { flex: 1, backgroundColor: p.ground },
    content: { padding: spacing.lg, gap: spacing.sm, paddingBottom: spacing.xl },

    // Identity header.
    header: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.md,
      marginBottom: spacing.sm,
    },
    headerText: { flex: 1, gap: 2 },
    headerNameRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
    headerName: {
      fontFamily: fonts.sansSemiBold,
      fontSize: 20,
      lineHeight: 26,
      color: p.fg,
      flexShrink: 1,
    },
    headerId: {
      fontFamily: fonts.mono,
      fontSize: 11,
      lineHeight: 15,
      color: p.fgMute,
    },
    defaultTag: {
      fontFamily: fonts.mono,
      fontSize: 9,
      lineHeight: 12,
      letterSpacing: 1,
      color: p.fg,
    },

    sectionLabel: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.9,
      color: p.fgMute,
      paddingHorizontal: spacing.xs,
      marginTop: spacing.md,
      marginBottom: spacing.xs,
    },

    // Grouped card.
    card: {
      backgroundColor: p.surface,
      borderRadius: radii.lg,
      borderWidth: 1,
      borderColor: p.edge,
      overflow: "hidden",
    },
    divider: {
      height: StyleSheet.hairlineWidth,
      backgroundColor: p.edge,
      marginLeft: spacing.lg,
    },
    infoRow: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "space-between",
      gap: spacing.md,
      paddingHorizontal: spacing.lg,
      paddingVertical: spacing.md,
      minHeight: 48,
    },
    rowPressed: { backgroundColor: p.raised },
    rowLabel: {
      fontFamily: fonts.sans,
      fontSize: 13,
      lineHeight: 18,
      color: p.fgMute,
    },
    rowValueWrap: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.xs,
      flexShrink: 1,
    },
    rowValue: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
      textAlign: "right",
      flexShrink: 1,
    },
    toolsBody: {},
    note: {
      fontFamily: fonts.sans,
      fontSize: 13,
      lineHeight: 18,
      color: p.fgMute,
    },
    footnote: {
      fontFamily: fonts.sans,
      fontSize: 12,
      lineHeight: 16,
      color: p.fgMute,
      paddingHorizontal: spacing.xs,
      marginTop: spacing.xs,
    },
    skeletonLine: {
      height: 16,
      borderRadius: radii.xs,
      backgroundColor: p.raised,
      marginHorizontal: spacing.lg,
      marginVertical: spacing.md,
    },

    // Inline name edit.
    editRow: { padding: spacing.md, gap: spacing.sm },
    editActions: {
      flexDirection: "row",
      justifyContent: "flex-end",
      gap: spacing.sm,
    },
    input: {
      backgroundColor: p.raised,
      borderWidth: 1,
      borderColor: p.edge,
      borderRadius: radii.md,
      paddingHorizontal: spacing.md,
      paddingVertical: 10,
      fontFamily: fonts.sans,
      fontSize: 15,
      lineHeight: 20,
      color: p.fg,
    },

    // Delete.
    deleteButton: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "center",
      gap: spacing.sm,
      marginTop: spacing.lg,
      paddingVertical: spacing.md,
      borderRadius: radii.md,
      borderWidth: 1,
      borderColor: p.alert,
      backgroundColor: p.raised,
    },
    deleteText: {
      fontFamily: fonts.sansMedium,
      fontSize: 14,
      lineHeight: 18,
      color: p.alert,
    },

    // Shared modal styling (mirrors agents.tsx create sheet).
    modalRoot: { flex: 1, justifyContent: "flex-end" },
    // A translucent dark scrim reads the same in both schemes.
    scrim: { flex: 1, backgroundColor: colors.scrim },
    sheet: {
      backgroundColor: p.ground,
      borderTopLeftRadius: radii.xl,
      borderTopRightRadius: radii.xl,
      padding: spacing.lg,
      paddingBottom: spacing.xl,
      gap: spacing.sm,
    },
    pickerSheet: { maxHeight: "70%" },
    sheetHeader: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "space-between",
      marginBottom: spacing.sm,
    },
    sheetTitle: {
      fontFamily: fonts.sansSemiBold,
      fontSize: 18,
      lineHeight: 24,
      color: p.fg,
    },
    pickerList: { flexGrow: 0 },
    pickerRow: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "space-between",
      gap: spacing.md,
      paddingVertical: spacing.md,
      paddingHorizontal: spacing.sm,
      borderRadius: radii.md,
    },
    pickerRowPressed: { backgroundColor: p.raised },
    pickerRowText: { flex: 1, gap: 2 },
    pickerName: {
      fontFamily: fonts.sansMedium,
      fontSize: 14,
      lineHeight: 18,
      color: p.fg,
    },
    pickerRef: {
      fontFamily: fonts.mono,
      fontSize: 11,
      lineHeight: 15,
      color: p.fgMute,
    },
    sheetActions: {
      flexDirection: "row",
      justifyContent: "flex-end",
      gap: spacing.sm,
      marginTop: spacing.md,
    },
    pressed: { opacity: 0.6 },
    ghostButton: {
      paddingHorizontal: spacing.lg,
      paddingVertical: 10,
      borderRadius: radii.md,
    },
    ghostButtonText: {
      fontFamily: fonts.sansMedium,
      fontSize: 14,
      lineHeight: 18,
      color: p.fgMute,
    },
    primaryButton: {
      minWidth: 92,
      alignItems: "center",
      justifyContent: "center",
      paddingHorizontal: spacing.lg,
      paddingVertical: 10,
      borderRadius: radii.md,
      backgroundColor: p.fg,
    },
    primaryButtonDisabled: { opacity: 0.5 },
    primaryButtonText: {
      fontFamily: fonts.sansMedium,
      fontSize: 14,
      lineHeight: 18,
      color: p.onFg,
    },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };
