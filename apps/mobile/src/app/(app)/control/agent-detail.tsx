import Feather from "@expo/vector-icons/Feather";
import { router, Stack, useLocalSearchParams } from "expo-router";
import { useCallback, useMemo, useState, type ReactNode } from "react";
import {
  ActivityIndicator,
  Alert,
  Animated,
  FlatList,
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import {
  agentDisplayName,
  getAgentModelString,
  useGatewayRpc,
  useGatewayRpcMutation,
  type Agent,
  type AgentsListResponse,
} from "@isol8/shared";

import { AgentMonogram } from "../../../components/control/AgentMonogram";
import { usePulse } from "../../../components/chat/usePulse";
import { colors, fonts, radii, spacing, type } from "../../../config/theme";
import {
  buildModelNameMap,
  buildSelectorModels,
  displayModelName,
  type CatalogModel,
} from "../../../lib/agentCatalog";
import { useGateway } from "../../../providers/gateway";

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
 *  deployments that inline it). */
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
  const [manual, setManual] = useState("");
  const catalogEmpty = models.length === 0;

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <KeyboardAvoidingView
        style={styles.modalRoot}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
      >
        <Pressable style={styles.scrim} onPress={onClose} />
        <View style={[styles.sheet, styles.pickerSheet]}>
          <View style={styles.sheetHeader}>
            <Text style={styles.sheetTitle}>Model</Text>
            <Pressable onPress={onClose} hitSlop={8} accessibilityLabel="Close">
              <Feather name="x" size={18} color={colors.mute} />
            </Pressable>
          </View>

          {catalogEmpty ? (
            <View style={styles.pickerManual}>
              <Text style={styles.note}>
                No models are configured in the gateway. Enter a model ref
                manually.
              </Text>
              <TextInput
                style={styles.input}
                value={manual}
                onChangeText={setManual}
                placeholder="amazon-bedrock/…"
                placeholderTextColor={colors.placeholder}
                autoCapitalize="none"
                autoCorrect={false}
              />
              <View style={styles.sheetActions}>
                <Pressable
                  style={({ pressed }) => [styles.ghostButton, pressed && styles.pressed]}
                  onPress={() => onSelect(null)}
                >
                  <Text style={styles.ghostButtonText}>Use default</Text>
                </Pressable>
                <Pressable
                  style={({ pressed }) => [
                    styles.primaryButton,
                    !manual.trim() && styles.primaryButtonDisabled,
                    pressed && !!manual.trim() && styles.pressed,
                  ]}
                  onPress={() => manual.trim() && onSelect(manual.trim())}
                  disabled={!manual.trim()}
                >
                  <Text style={styles.primaryButtonText}>Save</Text>
                </Pressable>
              </View>
            </View>
          ) : (
            <FlatList
              data={models}
              keyExtractor={(m) => m.ref}
              style={styles.pickerList}
              ListHeaderComponent={
                <Pressable
                  style={({ pressed }) => [
                    styles.pickerRow,
                    pressed && styles.pickerRowPressed,
                  ]}
                  onPress={() => onSelect(null)}
                >
                  <View style={styles.pickerRowText}>
                    <Text style={styles.pickerName}>Use default</Text>
                    <Text style={styles.pickerRef}>Inherit the gateway default</Text>
                  </View>
                  {currentRef == null && (
                    <Feather name="check" size={18} color={colors.forest} />
                  )}
                </Pressable>
              }
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
                      <Feather name="check" size={18} color={colors.forest} />
                    )}
                  </Pressable>
                );
              }}
            />
          )}
        </View>
      </KeyboardAvoidingView>
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
          <Feather name="chevron-right" size={16} color={colors.muteSoft} />
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
  const { agentId, name: paramName } = useLocalSearchParams<{
    agentId: string;
    name?: string;
  }>();
  const gateway = useGateway();
  const callRpc = useGatewayRpcMutation(gateway);

  const { data: agentsData, mutate: mutateAgents } =
    useGatewayRpc<AgentsListResponse>(gateway, "agents.list");
  const { data: configSnapshot, mutate: mutateConfig } = useGatewayRpc<unknown>(
    gateway,
    "config.get",
  );

  const agent: Agent | undefined = useMemo(
    () => agentsData?.agents?.find((a) => a.id === agentId),
    [agentsData, agentId],
  );
  const defaultId = agentsData?.defaultId;
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
    if (!agent || isDefault) return;
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
  }, [agent, isDefault, displayName, callRpc, mutateAgents]);

  const paperclip = configEntry?.params?.paperclip;
  const tools = configEntry?.tools;
  const workspace = configEntry?.workspace;
  const configLoading = configSnapshot === undefined;

  return (
    <ScrollView
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
                  <ActivityIndicator size="small" color={colors.white} />
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
              <ActivityIndicator size="small" color={colors.mute} />
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
        Tool and channel editing lives in the web control panel on isol8.co.
      </Text>

      {/* Danger zone */}
      {!isDefault && agent ? (
        <Pressable
          style={({ pressed }) => [styles.deleteButton, pressed && styles.pressed]}
          onPress={confirmDelete}
          accessibilityRole="button"
          accessibilityLabel="Delete agent"
        >
          <Feather name="trash-2" size={16} color={colors.danger} />
          <Text style={styles.deleteText}>Delete agent</Text>
        </Pressable>
      ) : isDefault ? (
        <Text style={styles.footnote}>
          The default agent is protected and can’t be deleted.
        </Text>
      ) : null}

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

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
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
  headerName: { ...type.title, flexShrink: 1 },
  headerId: {
    fontFamily: fonts.mono,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
  defaultTag: {
    fontFamily: fonts.mono,
    fontSize: 9,
    lineHeight: 12,
    letterSpacing: 1,
    color: colors.forest,
  },

  sectionLabel: {
    ...type.overline,
    paddingHorizontal: spacing.xs,
    marginTop: spacing.md,
    marginBottom: spacing.xs,
  },

  // Grouped white card.
  card: {
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    overflow: "hidden",
  },
  divider: {
    height: StyleSheet.hairlineWidth,
    backgroundColor: colors.edge,
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
  rowPressed: { backgroundColor: colors.creamPressed },
  rowLabel: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.mute,
  },
  rowValueWrap: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    flexShrink: 1,
  },
  rowValue: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
    textAlign: "right",
    flexShrink: 1,
  },
  toolsBody: {},
  note: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.mute,
  },
  footnote: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
    paddingHorizontal: spacing.xs,
    marginTop: spacing.xs,
  },
  skeletonLine: {
    height: 16,
    borderRadius: radii.xs,
    backgroundColor: colors.skeleton,
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
    backgroundColor: colors.white,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: 10,
    fontFamily: fonts.dmSans,
    fontSize: 15,
    lineHeight: 20,
    color: colors.ink,
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
    borderColor: colors.errorBorder,
    backgroundColor: colors.errorBg,
  },
  deleteText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 14,
    lineHeight: 18,
    color: colors.danger,
  },

  // Shared modal styling (mirrors agents.tsx create sheet).
  modalRoot: { flex: 1, justifyContent: "flex-end" },
  scrim: { flex: 1, backgroundColor: colors.scrim },
  sheet: {
    backgroundColor: colors.cream,
    borderTopLeftRadius: radii.xl,
    borderTopRightRadius: radii.xl,
    padding: spacing.lg,
    paddingBottom: spacing.xl,
    gap: spacing.sm,
  },
  pickerSheet: { maxHeight: "70%" },
  pickerManual: { gap: spacing.sm },
  sheetHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: spacing.sm,
  },
  sheetTitle: { ...type.title, fontSize: 18, lineHeight: 24 },
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
  pickerRowPressed: { backgroundColor: colors.creamPressed },
  pickerRowText: { flex: 1, gap: 2 },
  pickerName: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 14,
    lineHeight: 18,
    color: colors.ink,
  },
  pickerRef: {
    fontFamily: fonts.mono,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
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
    fontFamily: fonts.dmSansMedium,
    fontSize: 14,
    lineHeight: 18,
    color: colors.mute,
  },
  primaryButton: {
    minWidth: 92,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: spacing.lg,
    paddingVertical: 10,
    borderRadius: radii.md,
    backgroundColor: colors.forest,
  },
  primaryButtonDisabled: { backgroundColor: colors.creamPressed },
  primaryButtonText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 14,
    lineHeight: 18,
    color: colors.white,
  },
});
