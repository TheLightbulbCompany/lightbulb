import Feather from "@expo/vector-icons/Feather";
import { router, Stack } from "expo-router";
import { useCallback, useMemo, useState, type ReactElement } from "react";
import {
  ActivityIndicator,
  Alert,
  Animated,
  FlatList,
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
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

import { AgentMonogram, AgentStatusDot } from "../../../components/control/AgentMonogram";
import { usePulse } from "../../../components/chat/usePulse";
import { colors, fonts, radii, spacing, type } from "../../../config/theme";
import { deriveWorkspace, validateAgentName } from "../../../lib/agentCatalog";
import { useGateway } from "../../../providers/gateway";

// ---------------------------------------------------------------------------
// Skeleton (spec §7) — three pulsing rows while agents.list is in flight.
// ---------------------------------------------------------------------------

function SkeletonRows() {
  const pulse = usePulse(true, 0.55);
  return (
    <View style={styles.skeletonGroup}>
      {[0, 1, 2].map((i) => (
        <Animated.View key={i} style={[styles.skeletonRow, { opacity: pulse }]} />
      ))}
    </View>
  );
}

// ---------------------------------------------------------------------------
// Create-agent modal (spec §4 form). Name required; emoji optional (rides as
// params.paperclip.icon). agents.create runs a long serial chain server-side
// (Paperclip + 2 container RPCs + device pairing) that routinely exceeds the
// generic 30s budget while still succeeding — so a 90s timeout, and the
// caller always revalidates agents.list afterward (create is NON-idempotent:
// a re-click mints a SECOND agent, so submit latches while in flight).
// ---------------------------------------------------------------------------

function CreateAgentModal({
  visible,
  existingNames,
  onClose,
  onCreated,
}: {
  visible: boolean;
  existingNames: string[];
  onClose: () => void;
  onCreated: () => Promise<void>;
}) {
  const gateway = useGateway();
  const callRpc = useGatewayRpcMutation(gateway);
  const [name, setName] = useState("");
  const [emoji, setEmoji] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const clientError = validateAgentName(name, existingNames);
  const canCreate = name.trim() !== "" && clientError === null && !creating;

  const reset = useCallback(() => {
    setName("");
    setEmoji("");
    setError(null);
    setCreating(false);
  }, []);

  const handleClose = useCallback(() => {
    if (creating) return; // never dismiss mid-create (non-idempotent)
    reset();
    onClose();
  }, [creating, reset, onClose]);

  const handleCreate = useCallback(async () => {
    if (!canCreate) return;
    setCreating(true);
    setError(null);
    const trimmedEmoji = emoji.trim();
    const paperclip: Record<string, string> = { role: "general" };
    if (trimmedEmoji) paperclip.icon = trimmedEmoji;
    try {
      await callRpc(
        "agents.create",
        {
          name: name.trim(),
          // Explicit workspace: agents.create does NOT inherit
          // agents.defaults.workspace — pass the same path the default
          // resolves to at runtime so the agent lands on EFS.
          workspace: deriveWorkspace(name),
          reasoningDefault: "stream",
          params: { paperclip },
        },
        90_000,
      );
      // Success: always refetch the list before closing.
      await onCreated();
      reset();
      onClose();
    } catch (err) {
      // Even a create that blows past 90s may still succeed server-side —
      // revalidate so the agent surfaces rather than vanishing (and the user
      // does not re-click). Keep the modal open showing the error.
      await onCreated();
      setError(err instanceof Error ? err.message : String(err));
      setCreating(false);
    }
  }, [canCreate, callRpc, name, emoji, onCreated, onClose, reset]);

  return (
    <Modal
      visible={visible}
      transparent
      animationType="fade"
      onRequestClose={handleClose}
    >
      <KeyboardAvoidingView
        style={styles.modalRoot}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
      >
        <Pressable style={styles.scrim} onPress={handleClose} />
        <View style={styles.sheet}>
          <View style={styles.sheetHeader}>
            <Text style={styles.sheetTitle}>New agent</Text>
            <Pressable
              onPress={handleClose}
              hitSlop={8}
              disabled={creating}
              accessibilityRole="button"
              accessibilityLabel="Close"
            >
              <Feather name="x" size={18} color={colors.mute} />
            </Pressable>
          </View>

          <Text style={styles.fieldLabel}>NAME</Text>
          <TextInput
            style={styles.input}
            value={name}
            onChangeText={(t) => {
              setName(t);
              setError(null);
            }}
            placeholder="e.g. Research Assistant"
            placeholderTextColor={colors.placeholder}
            editable={!creating}
            autoFocus
            autoCapitalize="words"
            returnKeyType="done"
          />

          <Text style={styles.fieldLabel}>EMOJI (OPTIONAL)</Text>
          <TextInput
            style={styles.input}
            value={emoji}
            onChangeText={setEmoji}
            placeholder="🤖"
            placeholderTextColor={colors.placeholder}
            editable={!creating}
            maxLength={4}
          />

          {(clientError || error) && name.trim() !== "" && (
            <Text style={styles.formError}>{clientError || error}</Text>
          )}

          <View style={styles.sheetActions}>
            <Pressable
              style={({ pressed }) => [
                styles.ghostButton,
                pressed && styles.pressed,
              ]}
              onPress={handleClose}
              disabled={creating}
            >
              <Text style={styles.ghostButtonText}>Cancel</Text>
            </Pressable>
            <Pressable
              style={({ pressed }) => [
                styles.primaryButton,
                (!canCreate || creating) && styles.primaryButtonDisabled,
                pressed && canCreate && styles.pressed,
              ]}
              onPress={handleCreate}
              disabled={!canCreate}
              accessibilityRole="button"
              accessibilityLabel="Create agent"
            >
              {creating ? (
                <ActivityIndicator size="small" color={colors.white} />
              ) : (
                <Text style={styles.primaryButtonText}>Create</Text>
              )}
            </Pressable>
          </View>
        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function AgentsPanel() {
  const gateway = useGateway();
  const callRpc = useGatewayRpcMutation(gateway);
  const { data, error, isLoading, mutate } = useGatewayRpc<AgentsListResponse>(
    gateway,
    "agents.list",
  );

  const agents = useMemo(() => data?.agents ?? [], [data]);
  const defaultId = data?.defaultId;
  const existingNames = useMemo(
    () => agents.map((a) => agentDisplayName(a)),
    [agents],
  );

  const [createOpen, setCreateOpen] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const connectionDown = !gateway.isConnected && gateway.error != null;
  const showEmpty = agents.length === 0 && !isLoading && data != null;

  const confirmDelete = useCallback(
    (agent: Agent) => {
      // The default agent (Friday) is protected — never deletable in the UI.
      if (agent.id === defaultId) return;
      const label = agentDisplayName(agent);
      Alert.alert(
        `Delete ${label}?`,
        "This removes the agent and its workspace files. This cannot be undone.",
        [
          { text: "Cancel", style: "cancel" },
          {
            text: "Delete",
            style: "destructive",
            onPress: () => {
              setDeletingId(agent.id);
              void callRpc("agents.delete", {
                agentId: agent.id,
                deleteFiles: true,
              })
                .then(() => mutate())
                .catch((err: unknown) => {
                  Alert.alert(
                    "Couldn’t delete agent",
                    err instanceof Error ? err.message : String(err),
                  );
                })
                .finally(() => setDeletingId(null));
            },
          },
        ],
      );
    },
    [callRpc, defaultId, mutate],
  );

  const renderItem = useCallback(
    ({ item }: { item: Agent }) => {
      const name = agentDisplayName(item);
      const model = getAgentModelString(item);
      const isDefault = item.id === defaultId;
      const isDeleting = deletingId === item.id;
      return (
        <Pressable
          style={({ pressed }) => [
            styles.row,
            pressed && styles.rowPressed,
            isDeleting && styles.rowDeleting,
          ]}
          onPress={() =>
            router.push({
              pathname: "/(app)/control/agent-detail",
              params: { agentId: item.id, name },
            })
          }
          onLongPress={() => confirmDelete(item)}
          disabled={isDeleting}
          accessibilityRole="button"
          accessibilityLabel={`${name}. Tap to inspect, long-press to delete.`}
        >
          <AgentMonogram name={name} emoji={item.identity?.emoji ?? undefined} />
          <View style={styles.rowText}>
            <View style={styles.nameRow}>
              <Text style={styles.name} numberOfLines={1}>
                {name}
              </Text>
              {isDefault && <Text style={styles.defaultTag}>DEFAULT</Text>}
            </View>
            <Text style={styles.model} numberOfLines={1}>
              {model ?? "Uses default model"}
            </Text>
          </View>
          {isDeleting ? (
            <ActivityIndicator size="small" color={colors.mute} />
          ) : (
            <AgentStatusDot />
          )}
        </Pressable>
      );
    },
    [confirmDelete, defaultId, deletingId],
  );

  let body: ReactElement;
  if (error) {
    body = (
      <View style={styles.errorCard}>
        <Text style={styles.errorText}>
          Couldn’t load agents: {error.message}
        </Text>
        <Pressable
          style={({ pressed }) => [
            styles.retryButton,
            pressed && styles.pressed,
          ]}
          onPress={() => void mutate()}
          accessibilityRole="button"
          accessibilityLabel="Retry"
        >
          <Text style={styles.retryText}>Retry</Text>
        </Pressable>
      </View>
    );
  } else if (!data && connectionDown) {
    body = (
      <View style={styles.centerGrow}>
        <Text style={styles.dim}>{gateway.error}</Text>
      </View>
    );
  } else {
    body = (
      <FlatList
        data={agents}
        keyExtractor={(a) => a.id}
        style={styles.list}
        contentContainerStyle={[
          styles.listContent,
          showEmpty && styles.centerGrow,
        ]}
        ListHeaderComponent={
          agents.length > 0 ? (
            <Text style={styles.sectionLabel}>
              AGENTS
              <Text style={styles.sectionCount}> · {agents.length}</Text>
            </Text>
          ) : null
        }
        ListEmptyComponent={
          isLoading || !data ? (
            <SkeletonRows />
          ) : (
            <View style={styles.emptyWrap}>
              <Text style={styles.emptyGlyph}>8</Text>
              <Text style={styles.dim}>No agents yet.</Text>
            </View>
          )
        }
        renderItem={renderItem}
      />
    );
  }

  return (
    <View style={styles.page}>
      <Stack.Screen
        options={{
          title: "Agents",
          headerRight: () => (
            <Pressable
              style={styles.headerAction}
              onPress={() => setCreateOpen(true)}
              hitSlop={8}
              accessibilityRole="button"
              accessibilityLabel="New agent"
            >
              <Feather name="plus" size={22} color={colors.forest} />
            </Pressable>
          ),
        }}
      />
      {body}
      <CreateAgentModal
        visible={createOpen}
        existingNames={existingNames}
        onClose={() => setCreateOpen(false)}
        onCreated={async () => {
          await mutate();
        }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  list: { flex: 1 },
  listContent: {
    paddingHorizontal: spacing.md,
    paddingTop: spacing.md,
    paddingBottom: spacing.xl,
  },
  centerGrow: {
    flexGrow: 1,
    alignItems: "center",
    justifyContent: "center",
    padding: spacing.xl,
  },
  dim: { ...type.body, color: colors.mute, textAlign: "center" },

  headerAction: {
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.xs,
  },

  // Section label (spec §5).
  sectionLabel: {
    ...type.overline,
    paddingHorizontal: spacing.sm,
    marginBottom: spacing.sm,
  },
  sectionCount: { color: colors.sectionCount },

  // Roster rows (spec §5) — mirrors chat.tsx.
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    padding: spacing.sm,
    borderRadius: 10,
  },
  rowPressed: { backgroundColor: colors.rowPressed },
  rowDeleting: { opacity: 0.5 },
  rowText: { flex: 1 },
  nameRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  name: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
    flexShrink: 1,
  },
  defaultTag: {
    fontFamily: fonts.mono,
    fontSize: 9,
    lineHeight: 12,
    letterSpacing: 1,
    color: colors.forest,
  },
  model: {
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },

  // Empty state (spec §7).
  emptyWrap: { alignItems: "center", gap: spacing.sm },
  emptyGlyph: {
    fontFamily: fonts.loraItalic,
    fontSize: 48,
    lineHeight: 56,
    color: colors.ink,
  },

  // Loading skeletons (spec §7).
  skeletonGroup: { alignSelf: "stretch", gap: spacing.sm },
  skeletonRow: {
    height: 44,
    borderRadius: 10,
    backgroundColor: colors.skeleton,
  },

  // Error card (spec §4).
  errorCard: {
    margin: spacing.md,
    padding: spacing.lg,
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    gap: spacing.md,
  },
  errorText: {
    fontFamily: fonts.dmSans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.errorText,
  },
  retryButton: {
    alignSelf: "flex-start",
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.sm,
    backgroundColor: colors.white,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.xs,
  },
  retryText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
  },

  // Create modal (spec §4 sheet).
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
  sheetHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: spacing.sm,
  },
  sheetTitle: { ...type.title, fontSize: 18, lineHeight: 24 },
  fieldLabel: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1.4,
    color: colors.mute,
    marginTop: spacing.sm,
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
  formError: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.danger,
    marginTop: spacing.xs,
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
