import { SymbolView } from "expo-symbols";
import { router, Stack } from "expo-router";
import { useCallback, useMemo, useRef, useState, type ReactElement } from "react";
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
  useColorScheme,
  View,
} from "react-native";
import {
  agentDisplayName,
  getAgentModelString,
  useAgents,
  useRuntimeRpcMutation,
  type Agent,
} from "@lightbulb/shared";

import { AgentMonogram, AgentStatusDot } from "../../../components/control/AgentMonogram";
import { usePulse } from "../../../components/chat/usePulse";
import { chatPalette, colors, fonts, radii, spacing, type ChatPalette } from "../../../config/theme";
import { validateAgentName } from "../../../lib/agentCatalog";
import { useLiveEvents } from "../../../providers/live-events";
import { useContainerStatusContext } from "../../../lib/containerStatusContext";
import { isContainerComingUp } from "../../../lib/rosterPresentation";
import { LogoMark } from "../../../components/LogoMark";

// ---------------------------------------------------------------------------
// Skeleton (spec §7) — three pulsing rows while agents.list is in flight.
// ---------------------------------------------------------------------------

// `warming` names what we're waiting on when the wait is long enough to look
// like a hang — a container waking from zero. Mirrors the roster tab.
function SkeletonRows({ warming }: { warming?: boolean }) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const pulse = usePulse(true, 0.55);
  return (
    <View>
      {warming ? (
        <Text style={styles.warmingLabel}>Waking your agents…</Text>
      ) : null}
      <View style={styles.skeletonGroup}>
        {[0, 1, 2].map((i) => (
          <Animated.View key={i} style={[styles.skeletonRow, { opacity: pulse }]} />
        ))}
      </View>
    </View>
  );
}

// ---------------------------------------------------------------------------
// Create-agent modal (spec §4 form). Name required; emoji optional. Creation
// uses the shared operation-aware mutation and latches because a second submit
// would create a second agent.
// ---------------------------------------------------------------------------

function CreateAgentModal({
  visible,
  existingNames,
  onClose,
  onCreate,
}: {
  visible: boolean;
  existingNames: string[];
  onClose: () => void;
  onCreate: (params: { name: string; emoji?: string }) => Promise<void>;
}) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const [name, setName] = useState("");
  const [emoji, setEmoji] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Synchronous latch: `creating` state doesn't flush before a second tap's
  // onPress fires, so two sub-frame taps could both pass the state guard and
  // mint TWO agents (agents.create is non-idempotent). The ref closes that
  // window; it's set/cleared in lockstep with `creating`.
  const creatingRef = useRef(false);

  const clientError = validateAgentName(name, existingNames);
  const canCreate = name.trim() !== "" && clientError === null && !creating;

  const reset = useCallback(() => {
    setName("");
    setEmoji("");
    setError(null);
    creatingRef.current = false;
    setCreating(false);
  }, []);

  const handleClose = useCallback(() => {
    if (creating) return; // never dismiss mid-create (non-idempotent)
    reset();
    onClose();
  }, [creating, reset, onClose]);

  const handleCreate = useCallback(async () => {
    // Ref latch checked+set synchronously so a second sub-frame tap bails
    // before the `creating` re-render disables the button.
    if (!canCreate || creatingRef.current) return;
    creatingRef.current = true;
    setCreating(true);
    setError(null);
    const trimmedEmoji = emoji.trim();
    try {
      await onCreate({
        name: name.trim(),
        // No `workspace`: the backend fills the layout for the owner's runtime
        // (an EFS path on ECS, nothing on Hermes/AgentCore).
        ...(trimmedEmoji ? { emoji: trimmedEmoji } : {}),
      });
      reset();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      creatingRef.current = false;
      setCreating(false);
    }
  }, [canCreate, name, emoji, onCreate, onClose, reset]);

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
              <SymbolView accessibilityElementsHidden name="xmark" size={18} tintColor={palette.fgMute} />
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
            placeholderTextColor={palette.fgMute}
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
            placeholderTextColor={palette.fgMute}
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
                <ActivityIndicator size="small" color={palette.onFg} />
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
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const live = useLiveEvents();
  const callRpc = useRuntimeRpcMutation(live);
  // Roster reads and creation use the shared operation-aware hook. Deletion
  // stays local because this screen owns its confirmation flow.
  const { agents, defaultId, error, isLoading, isSettled, refresh, createAgent } =
    useAgents(live);
  const existingNames = useMemo(
    () => agents.map((a) => agentDisplayName(a)),
    [agents],
  );

  const [createOpen, setCreateOpen] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  // Same source of truth as the roster tab — and it has to be the SAME, not
  // merely similar: the server's container status decides whether a missing
  // roster is a wake or a failure (see isContainerComingUp).
  const containerStatus = useContainerStatusContext();
  const warming = isContainerComingUp(containerStatus);
  const hardError = error && !warming ? error : null;

  const renderErrorCard = (message: string): ReactElement => (
    <View style={styles.errorCard}>
      <Text style={styles.errorText}>Couldn’t load agents: {message}</Text>
      <Pressable
        style={({ pressed }) => [styles.retryButton, pressed && styles.pressed]}
        onPress={() => void refresh()}
        accessibilityRole="button"
        accessibilityLabel="Retry"
      >
        <Text style={styles.retryText}>Retry</Text>
      </Pressable>
    </View>
  );

  const showEmpty =
    agents.length === 0 && !isLoading && isSettled && !hardError;

  const confirmDelete = useCallback(
    (agent: Agent) => {
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
                .then(() => refresh())
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
    [callRpc, refresh],
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
            styles.card,
            pressed && styles.cardPressed,
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
          <View style={styles.cardTop}>
            <AgentMonogram name={name} emoji={item.identity?.emoji ?? undefined} />
            {isDeleting ? (
              <ActivityIndicator size="small" color={palette.fgMute} />
            ) : (
              <AgentStatusDot />
            )}
          </View>
          <Text style={styles.cardName} numberOfLines={2}>
            {name}
          </Text>
          <Text style={styles.cardModel} numberOfLines={1}>
            {model ?? "Uses default model"}
          </Text>
          {isDefault && <Text style={styles.defaultTag}>DEFAULT</Text>}
        </Pressable>
      );
    },
    [confirmDelete, defaultId, deletingId, palette, styles],
  );

  let body: ReactElement;
  // Only blank the screen when there is nothing to blank. Over a roster we
  // already fetched, the card rides above the tiles — losing the list, the
  // delete affordances and the New-agent entry point to a transient
  // revalidation failure is a worse answer than showing both.
  if (hardError && !isSettled) {
    body = renderErrorCard(hardError.message);
  } else {
    body = (
      <FlatList
        contentInsetAdjustmentBehavior="automatic"
        data={agents}
        keyExtractor={(a) => a.id}
        style={styles.list}
        numColumns={2}
        columnWrapperStyle={styles.columnWrap}
        contentContainerStyle={[
          styles.listContent,
          showEmpty && styles.centerGrow,
        ]}
        ListHeaderComponent={
          <>
            {hardError ? renderErrorCard(hardError.message) : null}
            {agents.length > 0 ? (
              <Text style={styles.sectionLabel}>
                AGENTS
                <Text style={styles.sectionCount}> · {agents.length}</Text>
              </Text>
            ) : null}
          </>
        }
        ListEmptyComponent={
          hardError ? null : isLoading || !isSettled ? (
            <SkeletonRows warming={warming} />
          ) : (
            <View style={styles.emptyWrap}>
              <LogoMark size={48} />
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
              <SymbolView accessibilityElementsHidden name="plus" size={22} tintColor={palette.fg} />
            </Pressable>
          ),
        }}
      />
      {body}
      <CreateAgentModal
        visible={createOpen}
        existingNames={existingNames}
        onClose={() => setCreateOpen(false)}
        onCreate={createAgent}
      />
    </View>
  );
}

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    page: { flex: 1, backgroundColor: p.ground },
    list: { flex: 1 },
    listContent: {
      paddingHorizontal: spacing.md,
      paddingTop: spacing.md,
      paddingBottom: spacing.xl,
      gap: spacing.sm,
    },
    // 2-up grid: each row's cards sit at the edges, odd trailing card stays
    // left-aligned at its own width (never stretched full-width).
    columnWrap: { justifyContent: "space-between" },
    centerGrow: {
      flexGrow: 1,
      alignItems: "center",
      justifyContent: "center",
      padding: spacing.xl,
    },
    dim: {
      fontFamily: fonts.sans,
      fontSize: 15,
      lineHeight: 22,
      color: p.fgMute,
      textAlign: "center",
    },

    headerAction: {
      paddingHorizontal: spacing.md,
      paddingVertical: spacing.xs,
    },

    // Section label (spec §5).
    sectionLabel: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.9,
      color: p.fgMute,
      paddingHorizontal: spacing.sm,
      marginBottom: spacing.sm,
    },
    sectionCount: { color: p.fgMute },

    // Agent grid cards — a 2-up roster (each card ~half width, edge-aligned).
    card: {
      width: "48%",
      backgroundColor: p.surface,
      borderWidth: 1,
      borderColor: p.edge,
      borderRadius: radii.md,
      padding: spacing.md,
      gap: spacing.xs,
      minHeight: 104,
    },
    cardPressed: { backgroundColor: p.raised },
    rowDeleting: { opacity: 0.5 },
    cardTop: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "space-between",
      marginBottom: spacing.xs,
    },
    cardName: {
      fontFamily: fonts.sansMedium,
      fontSize: 14,
      lineHeight: 19,
      color: p.fg,
    },
    cardModel: {
      fontFamily: fonts.sans,
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

    // Empty state (spec §7).
    emptyWrap: { alignItems: "center", gap: spacing.sm },

    // Loading skeletons (spec §7).
    warmingLabel: {
      fontFamily: fonts.sans,
      fontSize: 15,
      lineHeight: 22,
      color: p.fgMute,
      textAlign: "center",
      marginBottom: spacing.md,
    },
    skeletonGroup: { alignSelf: "stretch", gap: spacing.sm },
    skeletonRow: {
      height: 44,
      borderRadius: 10,
      backgroundColor: p.raised,
    },

    // Error card (spec §4).
    errorCard: {
      margin: spacing.md,
      padding: spacing.lg,
      backgroundColor: p.raised,
      borderWidth: 1,
      borderColor: p.alert,
      borderRadius: radii.md,
      gap: spacing.md,
    },
    errorText: {
      fontFamily: fonts.sans,
      fontSize: 14,
      lineHeight: 20,
      color: p.alert,
    },
    retryButton: {
      alignSelf: "flex-start",
      borderWidth: 1,
      borderColor: p.alert,
      borderRadius: radii.sm,
      backgroundColor: p.surface,
      paddingHorizontal: spacing.md,
      paddingVertical: spacing.xs,
    },
    retryText: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.alert,
    },

    // Create modal (spec §4 sheet).
    modalRoot: { flex: 1, justifyContent: "flex-end" },
    // A translucent dark scrim reads the same in both schemes — it dims
    // whatever is behind the sheet, not the sheet's own surface.
    scrim: { flex: 1, backgroundColor: colors.scrim },
    sheet: {
      backgroundColor: p.ground,
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
    sheetTitle: {
      fontFamily: fonts.sansSemiBold,
      fontSize: 18,
      lineHeight: 24,
      color: p.fg,
    },
    fieldLabel: {
      fontFamily: fonts.mono,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.4,
      color: p.fgMute,
      marginTop: spacing.sm,
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
    formError: {
      fontFamily: fonts.sans,
      fontSize: 12,
      lineHeight: 16,
      color: p.alert,
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
