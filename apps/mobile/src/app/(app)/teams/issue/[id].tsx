import { useUser } from "@clerk/expo";
import { SymbolView } from "../../../../components/ui/SymbolView";
import { Stack, router, useLocalSearchParams } from "expo-router";
import { useMemo, useState, type ReactElement, type ReactNode } from "react";
import {
  Animated,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  useColorScheme,
  View,
} from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { MessageMarkdown } from "../../../../components/chat/MessageMarkdown";
import { usePulse } from "../../../../components/chat/usePulse";
import { ApprovalCard } from "../../../../components/teams/ApprovalCard";
import { AssigneeAvatar } from "../../../../components/teams/AssigneeAvatar";
import { CommentComposer } from "../../../../components/teams/CommentComposer";
import { CreateIssueSheet } from "../../../../components/teams/CreateIssueSheet";
import { InteractionCard } from "../../../../components/teams/InteractionCard";
import {
  AttachmentsSection,
  DocumentsSection,
  WorkProductsSection,
} from "../../../../components/teams/IssueOutputsSection";
import { IssueRunsSection } from "../../../../components/teams/IssueRunsSection";
import { PickerSheet, type PickerOption } from "../../../../components/teams/PickerSheet";
import { ReasonSheet } from "../../../../components/teams/ReasonSheet";
import { chatPalette, fonts, radii, spacing, type ChatPalette } from "../../../../config/theme";
import {
  BOARD_STATUSES,
  PRIORITIES,
  PRIORITY_LABELS,
  STATUS_LABELS,
  commentAuthorLabel,
  humanizeStatus,
  looksLikeMarkdown,
  memberName,
  priorityColor,
  resolveAssignee,
  statusColors,
  documentChangeSummary,
  timeAgo,
  toBoardStatus,
  useTeamsIssue,
  type BoardStatus,
  type ChildIssue,
  type CompanyMember,
  type CreateIssueInput,
  type InteractionAnswer,
  type IssueAttachment,
  type IssueComment,
  type IssueDetail,
  type IssueDocument,
  type IssueInteraction,
  type IssueRun,
  type IssueWorkProduct,
  type Priority,
  type TeamsAgent,
  type TeamsApproval,
} from "../../../../lib/teamsData";
import { useStackedRows } from "../../../../lib/textScale";

const UNASSIGNED_KEY = "__unassigned__";
const AGENT_PREFIX = "agent:";
const USER_PREFIX = "user:";

// ---------------------------------------------------------------------------
// Loading / readiness affordances
// ---------------------------------------------------------------------------

function PulsingDot() {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const opacity = usePulse(true, 0.3);
  return <Animated.View style={[styles.pulseDot, { opacity }]} />;
}

function SkeletonBody() {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const pulse = usePulse(true, 0.55);
  return (
    <View style={styles.skeletonGroup}>
      <Animated.View style={[styles.skeletonLine, { opacity: pulse, width: "70%" }]} />
      <Animated.View style={[styles.skeletonLine, { opacity: pulse, width: "40%" }]} />
      <Animated.View style={[styles.skeletonCard, { opacity: pulse }]} />
      <Animated.View style={[styles.skeletonLine, { opacity: pulse, width: "55%" }]} />
    </View>
  );
}

function CenteredState({
  title,
  detail,
  action,
  pulsing,
}: {
  title: string;
  detail?: string | null;
  action?: ReactElement;
  pulsing?: boolean;
}) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  return (
    <View style={styles.centered}>
      {pulsing ? <PulsingDot /> : null}
      <Text style={styles.stateTitle}>{title}</Text>
      {detail != null && detail !== "" && (
        <Text style={styles.stateDetail}>{detail}</Text>
      )}
      {action}
    </View>
  );
}

function OutlineButton({ label, onPress }: { label: string; onPress: () => void }) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  return (
    <Pressable
      style={({ pressed }) => [styles.button, pressed && styles.buttonPressed]}
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={label}
    >
      <Text style={styles.buttonText}>{label}</Text>
    </Pressable>
  );
}

// ---------------------------------------------------------------------------
// Body pieces
// ---------------------------------------------------------------------------

/** Renders comment/description body as markdown or plain pre-wrap Text. */
function BodyText({ text }: { text: string }) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  if (looksLikeMarkdown(text)) {
    return <MessageMarkdown text={text} palette={chatPalette[scheme]} />;
  }
  return <Text style={styles.plainBody}>{text}</Text>;
}

/** A colored status dot — filled for decisive statuses, hollow ring otherwise. */
function StatusSwatch({ status }: { status: string | null | undefined }) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const { color } = statusColors(status, scheme);
  const key = toBoardStatus(status);
  const filled = key === "in_progress" || key === "done" || key === "blocked";
  return (
    <View
      style={[
        styles.swatch,
        { borderColor: color },
        filled ? { backgroundColor: color } : { backgroundColor: "transparent" },
      ]}
    />
  );
}

/** A static, non-editable property row (e.g. CREATED). */
function StaticRow({ label, value }: { label: string; value: string }) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const stacked = useStackedRows();
  return (
    <View style={[styles.propRow, stacked && styles.propRowStacked]}>
      <Text style={[styles.propLabel, stacked && styles.propLabelStacked]}>{label}</Text>
      <Text style={styles.propValue} numberOfLines={2}>
        {value}
      </Text>
    </View>
  );
}

/** A tappable property row that opens a picker; a chevron signals editability. */
function EditableRow({
  label,
  onPress,
  children,
}: {
  label: string;
  onPress: () => void;
  children: ReactNode;
}) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const stacked = useStackedRows();
  return (
    <Pressable
      style={({ pressed }) => [
        styles.propRow,
        styles.editRow,
        stacked && styles.propRowStacked,
        pressed && styles.editRowPressed,
      ]}
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={`Edit ${label.toLowerCase()}`}
    >
      <Text style={[styles.propLabel, stacked && styles.propLabelStacked]}>{label}</Text>
      {/* Value and chevron stay on one line even when the label moves above. */}
      <View style={[styles.editValue, stacked && styles.editValueStacked]}>
        {children}
        <SymbolView
          accessibilityElementsHidden
          name="chevron.down"
          size={15}
          tintColor={palette.fgMute}
        />
      </View>
    </Pressable>
  );
}

function CommentRow({
  comment,
  agents,
  members,
  currentUserId,
}: {
  comment: IssueComment;
  agents: TeamsAgent[];
  members: CompanyMember[];
  currentUserId: string | null;
}) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const pending = comment.id.startsWith("temp-");
  return (
    <View style={[styles.comment, pending && styles.commentPending]}>
      <View style={styles.commentMeta}>
        <Text style={styles.commentAuthor}>
          {commentAuthorLabel(comment, agents, members, currentUserId)}
        </Text>
        <Text style={styles.commentDot}>·</Text>
        <Text style={styles.commentTime}>
          {pending ? "Sending…" : timeAgo(comment.createdAt)}
        </Text>
      </View>
      <BodyText text={comment.body} />
    </View>
  );
}

/** A tappable sub-issue mini-card that recurses into its own detail. */
function ChildCard({ child }: { child: ChildIssue }) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const { color, tint } = statusColors(child.status, scheme);
  const key = toBoardStatus(child.status);
  const label = key
    ? STATUS_LABELS[key]
    : child.status
      ? humanizeStatus(child.status)
      : "No status";
  return (
    <Pressable
      style={({ pressed }) => [styles.childCard, pressed && styles.childCardPressed]}
      onPress={() =>
        router.push({
          pathname: "/(app)/teams/issue/[id]",
          params: { id: child.id, title: child.title },
        })
      }
      accessibilityRole="button"
      accessibilityLabel={`${child.title}. ${label}.`}
    >
      <StatusSwatch status={child.status} />
      <View style={styles.childText}>
        <Text style={styles.childIdent}>
          {child.identifier ?? child.id.slice(0, 8)}
        </Text>
        <Text style={styles.childTitle} numberOfLines={1}>
          {child.title}
        </Text>
      </View>
      <View style={[styles.childStatusChip, { backgroundColor: tint }]}>
        <Text style={[styles.childStatusText, { color }]} numberOfLines={1}>
          {label}
        </Text>
      </View>
    </Pressable>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function IssueDetailScreen() {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const insets = useSafeAreaInsets();
  const params = useLocalSearchParams<{ id: string; title?: string }>();
  const id = typeof params.id === "string" ? params.id : undefined;
  const seedTitle = typeof params.title === "string" ? params.title : undefined;
  const {
    state,
    retry,
    setStatus,
    setPriority,
    setAssignee,
    setUserAssignee,
    setTitle,
    setDescription,
    addComment,
    createChild,
    updateDocument,
    approveApproval,
    rejectApproval,
    acceptInteraction,
    rejectInteraction,
    respondInteraction,
    reviewWorkProduct,
  } = useTeamsIssue(id);
  const { user } = useUser();
  const currentUserId = user?.id ?? null;

  const headerTitle =
    state.kind === "ready" ? state.issue.title : seedTitle ?? "Issue";

  let body: ReactElement;
  if (state.kind === "loading") {
    body = <SkeletonBody />;
  } else if (state.kind === "gate") {
    if (state.gate === "provisioning") {
      body = (
        <CenteredState
          pulsing
          title="Setting up your workspace…"
          detail="This usually takes under a minute."
          action={<OutlineButton label="Refresh" onPress={retry} />}
        />
      );
    } else if (state.gate === "needs_subscription") {
      body = <CenteredState title="Teams isn't available for this workspace right now." />;
    } else {
      body = (
        <CenteredState
          title="Couldn’t reach your workspace"
          detail={state.message}
          action={<OutlineButton label="Retry" onPress={retry} />}
        />
      );
    }
  } else if (state.kind === "error") {
    body = (
      <View style={styles.errorCard}>
        <Text style={styles.errorText}>Couldn’t load this issue: {state.message}</Text>
        <OutlineButton label="Retry" onPress={retry} />
      </View>
    );
  } else {
    body = (
      <ReadyBody
        state={state}
        currentUserId={currentUserId}
        onSetStatus={setStatus}
        onSetPriority={setPriority}
        onSetAssignee={setAssignee}
        onSetUserAssignee={setUserAssignee}
        onSetTitle={setTitle}
        onSetDescription={setDescription}
        onCreateChild={createChild}
        onUpdateDocument={(key, title, body, baseRevisionId) =>
          updateDocument(key, title, body, documentChangeSummary(user), baseRevisionId)
        }
        onApproveApproval={approveApproval}
        onRejectApproval={rejectApproval}
        onAcceptInteraction={acceptInteraction}
        onRejectInteraction={rejectInteraction}
        onRespondInteraction={respondInteraction}
        onReviewWorkProduct={reviewWorkProduct}
      />
    );
  }

  return (
    <KeyboardAvoidingView
      style={styles.page}
      behavior="padding"
      // Measured, not guessed: the old `keyboardVerticalOffset={90}` stood in
      // for a header that is 103-116pt tall, so the comment box sat 13-26pt
      // under the keyboard. Same pairing as the room screen — the composer's
      // own `insets.bottom` padding tucks behind the keyboard.
      automaticOffset
      keyboardVerticalOffset={-insets.bottom}
    >
      <Stack.Screen options={{ title: headerTitle }} />
      <ScrollView
        style={styles.scroll}
        contentContainerStyle={styles.content}
        keyboardShouldPersistTaps="handled"
      >
        {body}
      </ScrollView>
      {state.kind === "ready" && <CommentComposer onSubmit={addComment} />}
    </KeyboardAvoidingView>
  );
}

type OpenPicker = "status" | "priority" | "assignee" | null;
type OpenEditor = "title" | "description" | null;

/** One thread entry — comments and agent interactions interleaved by time
 * (web IssueComments buildTeamsThreadItems parity). */
type ThreadItem =
  | { kind: "comment"; at: string; comment: IssueComment }
  | { kind: "interaction"; at: string; interaction: IssueInteraction };

function ReadyBody({
  state,
  currentUserId,
  onSetStatus,
  onSetPriority,
  onSetAssignee,
  onSetUserAssignee,
  onSetTitle,
  onSetDescription,
  onCreateChild,
  onUpdateDocument,
  onApproveApproval,
  onRejectApproval,
  onAcceptInteraction,
  onRejectInteraction,
  onRespondInteraction,
  onReviewWorkProduct,
}: {
  state: {
    issue: IssueDetail;
    comments: IssueComment[];
    children: ChildIssue[];
    agents: TeamsAgent[];
    members: CompanyMember[];
    approvals: TeamsApproval[];
    runs: IssueRun[];
    interactions: IssueInteraction[];
    workProducts: IssueWorkProduct[];
    documents: IssueDocument[];
    attachments: IssueAttachment[];
  };
  currentUserId: string | null;
  onSetStatus: (status: BoardStatus) => void;
  onSetPriority: (priority: Priority) => void;
  onSetAssignee: (agentId: string | null) => void;
  onSetUserAssignee: (userId: string | null) => void;
  onSetTitle: (title: string) => void;
  onSetDescription: (description: string) => void;
  onCreateChild: (input: CreateIssueInput) => Promise<boolean>;
  onUpdateDocument: (key: string, title: string, body: string, baseRevisionId: string | null) => Promise<void>;
  onApproveApproval: (approvalId: string) => void;
  onRejectApproval: (approvalId: string, reason: string) => void;
  onAcceptInteraction: (
    id: string,
    selection?: { selectedClientKeys?: string[]; selectedOptionIds?: string[] },
  ) => void;
  onRejectInteraction: (id: string, reason?: string) => void;
  onRespondInteraction: (id: string, answers: InteractionAnswer[]) => void;
  onReviewWorkProduct: (id: string, verdict: "approved" | "changes_requested") => void;
}) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const {
    issue,
    comments,
    children,
    agents,
    members,
    approvals,
    runs,
    interactions,
    workProducts,
    documents,
    attachments,
  } = state;
  const identifier = issue.identifier ?? issue.id.slice(0, 8);
  const assignee = resolveAssignee(issue, agents, members);
  const created = issue.createdAt ? timeAgo(issue.createdAt) : null;
  const prColor = priorityColor(issue.priority, scheme);

  const [picker, setPicker] = useState<OpenPicker>(null);
  const [editor, setEditor] = useState<OpenEditor>(null);
  const [addingChild, setAddingChild] = useState(false);

  // Comments + agent interactions interleaved chronologically — an agent
  // waiting on an answer must be visible IN the thread, not siloed (the old
  // comments-only thread silently hid pending interactions, so a blocked
  // agent read as "went quiet").
  const thread = useMemo<ThreadItem[]>(() => {
    const items: ThreadItem[] = [
      ...comments.map((comment) => ({
        kind: "comment" as const,
        at: comment.createdAt,
        comment,
      })),
      ...interactions.map((interaction) => ({
        kind: "interaction" as const,
        at: String(interaction.createdAt ?? ""),
        interaction,
      })),
    ];
    return items.sort((a, b) => a.at.localeCompare(b.at));
  }, [comments, interactions]);

  // Sectioned assignee options: Unassigned, then agent monograms, then people.
  // Keys are prefixed (`agent:`/`user:`) so onSelect routes to the right write
  // and only ONE of the mutually-exclusive fields is ever set (contract §1).
  const assigneeOptions = useMemo<PickerOption[]>(
    () => [
      { key: UNASSIGNED_KEY, label: "Unassigned" },
      ...agents.map((a) => ({
        key: `${AGENT_PREFIX}${a.id}`,
        label: a.name ?? "Agent",
        section: "Agents",
        leading: (
          <AssigneeAvatar assignee={{ name: a.name ?? "Agent", kind: "agent" }} size={20} />
        ),
      })),
      ...members.map((m) => {
        const name = memberName(m.userId, members) ?? "Member";
        return {
          key: `${USER_PREFIX}${m.userId}`,
          label: name,
          section: "People",
          leading: (
            <AssigneeAvatar
              assignee={{ name, kind: "user", imageUrl: m.imageUrl ?? null }}
              size={20}
            />
          ),
        };
      }),
    ],
    [agents, members],
  );

  const assigneeSelectedKey = issue.assigneeAgentId
    ? `${AGENT_PREFIX}${issue.assigneeAgentId}`
    : issue.assigneeUserId
      ? `${USER_PREFIX}${issue.assigneeUserId}`
      : UNASSIGNED_KEY;

  return (
    <>
      {/* Title + identifier — tap the title to edit it. */}
      <View style={styles.headerBlock}>
        <Text style={styles.identifier}>{identifier}</Text>
        <Pressable
          onPress={() => setEditor("title")}
          accessibilityRole="button"
          accessibilityLabel="Edit title"
        >
          <Text style={styles.issueTitle}>
            {issue.title}
            <Text style={styles.editGlyph}>  ✎</Text>
          </Text>
        </Pressable>
      </View>

      {/* Properties — status/priority/agent are tappable → picker sheets. */}
      <View style={styles.card}>
        <EditableRow label="STATUS" onPress={() => setPicker("status")}>
          <StatusSwatch status={issue.status} />
          <Text style={styles.editValueText} numberOfLines={1}>
            {humanizeStatus(issue.status)}
          </Text>
        </EditableRow>

        <EditableRow label="PRIORITY" onPress={() => setPicker("priority")}>
          {prColor ? <View style={[styles.priorityDot, { backgroundColor: prColor }]} /> : null}
          <Text style={styles.editValueText} numberOfLines={1}>
            {issue.priority ? humanizeStatus(issue.priority) : "None"}
          </Text>
        </EditableRow>

        <EditableRow label="ASSIGNEE" onPress={() => setPicker("assignee")}>
          {assignee != null ? <AssigneeAvatar assignee={assignee} size={20} /> : null}
          <Text style={styles.editValueText} numberOfLines={1}>
            {assignee?.name ?? "Unassigned"}
          </Text>
        </EditableRow>

        {created != null && <StaticRow label="CREATED" value={created} />}
      </View>

      {/* Description — pencil edits (or adds) it. */}
      <View style={styles.section}>
        <View style={styles.sectionHeadRow}>
          <Text style={styles.sectionLabel}>DESCRIPTION</Text>
          <Pressable
            onPress={() => setEditor("description")}
            hitSlop={8}
            accessibilityRole="button"
            accessibilityLabel="Edit description"
          >
            <SymbolView accessibilityElementsHidden name="pencil" size={13} tintColor={palette.fgMute} />
          </Pressable>
        </View>
        {issue.description != null && issue.description.trim() !== "" ? (
          <BodyText text={issue.description} />
        ) : (
          <Text style={styles.emptyComments}>No description.</Text>
        )}
      </View>

      {/* Approvals — the issue's approval requests with inline Approve/Reject. */}
      {approvals.length > 0 && (
        <View style={styles.section}>
          <Text style={styles.sectionLabel}>
            APPROVALS
            <Text style={styles.sectionCount}> · {approvals.length}</Text>
          </Text>
          <View style={styles.approvalList}>
            {approvals.map((approval) => (
              <ApprovalCard
                key={approval.id}
                approval={approval}
                agents={agents}
                members={members}
                onApprove={onApproveApproval}
                onReject={onRejectApproval}
              />
            ))}
          </View>
        </View>
      )}

      {/* Agent runs against this issue (web IssueRunsSection parity). */}
      <IssueRunsSection runs={runs} agents={agents} />

      {/* Outputs — work products (with board review), documents, attachments. */}
      <WorkProductsSection workProducts={workProducts} onReview={onReviewWorkProduct} />
      <DocumentsSection
        documents={documents}
        agents={agents}
        members={members}
        currentUserId={currentUserId}
        onSave={onUpdateDocument}
      />
      <AttachmentsSection issueId={state.issue.id} attachments={attachments} />

      {/* Sub-issues — interactive mini-cards + add affordance. */}
      <View style={styles.section}>
        <Text style={styles.sectionLabel}>
          SUB-ISSUES
          {children.length > 0 && (
            <Text style={styles.sectionCount}> · {children.length}</Text>
          )}
        </Text>
        {children.length > 0 && (
          <View style={styles.childList}>
            {children.map((child) => (
              <ChildCard key={child.id} child={child} />
            ))}
          </View>
        )}
        <Pressable
          style={({ pressed }) => [styles.addChild, pressed && styles.addChildPressed]}
          onPress={() => setAddingChild(true)}
          accessibilityRole="button"
          accessibilityLabel="Add sub-issue"
        >
          <SymbolView accessibilityElementsHidden name="plus" size={15} tintColor={palette.fg} />
          <Text style={styles.addChildText}>Add sub-issue</Text>
        </Pressable>
      </View>

      {/* Thread — comments AND agent interactions, chronological. */}
      <View style={styles.section}>
        <Text style={styles.sectionLabel}>
          COMMENTS
          {thread.length > 0 && (
            <Text style={styles.sectionCount}> · {thread.length}</Text>
          )}
        </Text>
        {thread.length === 0 ? (
          <Text style={styles.emptyComments}>No comments yet.</Text>
        ) : (
          <View style={styles.commentList}>
            {thread.map((item) =>
              item.kind === "comment" ? (
                <CommentRow
                  key={`c-${item.comment.id}`}
                  comment={item.comment}
                  agents={agents}
                  members={members}
                  currentUserId={currentUserId}
                />
              ) : (
                <InteractionCard
                  key={`i-${item.interaction.id}`}
                  interaction={item.interaction}
                  agents={agents}
                  onAccept={onAcceptInteraction}
                  onReject={onRejectInteraction}
                  onRespond={onRespondInteraction}
                />
              ),
            )}
          </View>
        )}
      </View>

      {/* Status picker */}
      <PickerSheet
        visible={picker === "status"}
        heading="Set status"
        subheading={issue.title}
        options={BOARD_STATUSES.map((s) => ({
          key: s,
          label: STATUS_LABELS[s],
          dotColor: statusColors(s, scheme).color,
          strikethrough: s === "cancelled",
        }))}
        selectedKey={issue.status ?? null}
        onSelect={(key) => onSetStatus(key as BoardStatus)}
        onClose={() => setPicker(null)}
      />

      {/* Priority picker */}
      <PickerSheet
        visible={picker === "priority"}
        heading="Set priority"
        subheading={issue.title}
        options={PRIORITIES.map((p) => ({
          key: p,
          label: PRIORITY_LABELS[p],
          dotColor: priorityColor(p, scheme),
        }))}
        selectedKey={issue.priority ?? null}
        onSelect={(key) => onSetPriority(key as Priority)}
        onClose={() => setPicker(null)}
      />

      {/* Assignee picker — agents + people, mutually exclusive (contract §1). */}
      <PickerSheet
        visible={picker === "assignee"}
        heading="Assign"
        subheading={issue.title}
        options={assigneeOptions}
        selectedKey={assigneeSelectedKey}
        onSelect={(key) => {
          if (key === UNASSIGNED_KEY) {
            onSetAssignee(null);
          } else if (key.startsWith(AGENT_PREFIX)) {
            onSetAssignee(key.slice(AGENT_PREFIX.length));
          } else if (key.startsWith(USER_PREFIX)) {
            onSetUserAssignee(key.slice(USER_PREFIX.length));
          }
        }}
        onClose={() => setPicker(null)}
      />

      {/* Create sub-issue */}
      <CreateIssueSheet
        visible={addingChild}
        mode="subissue"
        onSubmit={onCreateChild}
        onClose={() => setAddingChild(false)}
      />

      {/* Title / description edit sheets. */}
      <ReasonSheet
        visible={editor === "title"}
        title="Edit title"
        placeholder="Title"
        submitLabel="Save"
        tone="primary"
        initialValue={issue.title}
        onSubmit={onSetTitle}
        onClose={() => setEditor(null)}
      />
      <ReasonSheet
        visible={editor === "description"}
        title="Edit description"
        placeholder="Description"
        submitLabel="Save"
        tone="primary"
        required={false}
        initialValue={issue.description ?? ""}
        onSubmit={onSetDescription}
        onClose={() => setEditor(null)}
      />
    </>
  );
}

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    page: { flex: 1, backgroundColor: p.ground },
    scroll: { flex: 1 },
    content: { padding: spacing.lg, gap: spacing.lg, paddingBottom: spacing.xl },

    // Header block.
    headerBlock: { gap: spacing.xs },
    identifier: {
      fontFamily: fonts.mono,
      fontSize: 11,
      letterSpacing: 1,
      color: p.fgMute,
    },
    issueTitle: {
      fontFamily: fonts.sansSemiBold,
      fontSize: 18,
      lineHeight: 24,
      color: p.fg,
    },

    // Property card.
    card: {
      backgroundColor: p.surface,
      borderRadius: radii.lg,
      borderWidth: 1,
      borderColor: p.edge,
      padding: spacing.sm,
      gap: spacing.xs,
    },
    propRow: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.md,
      paddingHorizontal: spacing.sm,
      paddingVertical: spacing.sm,
    },
    editRow: { borderRadius: radii.md },
    editRowPressed: { backgroundColor: p.raised },
    propLabel: {
      fontFamily: fonts.mono,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.4,
      color: p.fgMute,
      width: 72,
    },
    // At accessibility text sizes the label takes its own line, the way UIKit's
    // own list rows do — a 72pt column breaks "PRIORITY" mid-word.
    propRowStacked: { flexDirection: "column", alignItems: "stretch", gap: spacing.xs },
    propLabelStacked: { width: "auto" },
    editValueStacked: { width: "100%" },
    propValue: {
      flex: 1,
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
    },
    editValue: {
      flex: 1,
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.sm,
    },
    editValueText: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
      // Fills the row so the chevron stays pinned to its right edge.
      flex: 1,
    },
    swatch: { width: 12, height: 12, borderRadius: 6, borderWidth: 1.5 },
    priorityDot: { width: 10, height: 10, borderRadius: 5 },

    // Sections.
    section: { gap: spacing.sm },
    sectionLabel: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.9,
      color: p.fgMute,
    },
    sectionCount: { color: p.fgMute },
    sectionHeadRow: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "space-between",
    },
    editGlyph: { color: p.fgMute, fontSize: 13 },

    plainBody: {
      fontFamily: fonts.sans,
      fontSize: 14,
      lineHeight: 22,
      color: p.fg,
    },

    // Approval cards.
    approvalList: { gap: spacing.sm },

    // Sub-issue cards.
    childList: { gap: spacing.sm },
    childCard: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.sm,
      backgroundColor: p.surface,
      borderRadius: radii.md,
      borderWidth: 1,
      borderColor: p.edge,
      paddingHorizontal: spacing.md,
      paddingVertical: spacing.sm,
    },
    childCardPressed: { backgroundColor: p.raised },
    childText: { flex: 1, gap: 2 },
    childIdent: {
      fontFamily: fonts.mono,
      fontSize: 10,
      letterSpacing: 0.6,
      color: p.fgMute,
    },
    childTitle: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
    },
    childStatusChip: {
      paddingHorizontal: spacing.sm,
      paddingVertical: 3,
      borderRadius: radii.sm,
      maxWidth: 96,
    },
    childStatusText: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
    },
    addChild: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.xs,
      alignSelf: "flex-start",
      paddingHorizontal: spacing.md,
      paddingVertical: spacing.sm,
      borderRadius: radii.sm,
      borderWidth: 1,
      borderColor: p.edge,
      borderStyle: "dashed",
      backgroundColor: p.surface,
    },
    addChildPressed: { backgroundColor: p.raised },
    addChildText: {
      fontFamily: fonts.sansMedium,
      fontSize: 12,
      lineHeight: 16,
      color: p.fg,
    },

    // Comments.
    commentList: { gap: spacing.lg },
    comment: {
      gap: spacing.xs,
      paddingLeft: spacing.md,
      borderLeftWidth: 2,
      borderLeftColor: p.edge,
    },
    commentPending: { opacity: 0.55 },
    commentMeta: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.sm,
    },
    commentAuthor: {
      fontFamily: fonts.sansMedium,
      fontSize: 12,
      lineHeight: 16,
      color: p.fgSoft,
    },
    commentDot: { fontFamily: fonts.sans, fontSize: 12, color: p.fgMute },
    commentTime: {
      fontFamily: fonts.sans,
      fontSize: 11,
      lineHeight: 16,
      color: p.fgMute,
    },
    emptyComments: {
      fontFamily: fonts.lora,
      fontSize: 14,
      lineHeight: 20,
      color: p.fgMute,
      fontStyle: "italic",
    },

    // States.
    centered: {
      alignItems: "center",
      justifyContent: "center",
      gap: spacing.md,
      padding: spacing.xl,
    },
    pulseDot: {
      width: 12,
      height: 12,
      borderRadius: 6,
      backgroundColor: p.fg,
    },
    stateTitle: {
      fontFamily: fonts.sansMedium,
      fontSize: 15,
      lineHeight: 20,
      color: p.fg,
      textAlign: "center",
    },
    stateDetail: {
      fontFamily: fonts.sans,
      fontSize: 13,
      lineHeight: 19,
      color: p.fgMute,
      textAlign: "center",
    },
    button: {
      marginTop: spacing.xs,
      borderWidth: 1,
      borderColor: p.edge,
      borderRadius: radii.sm,
      backgroundColor: p.surface,
      paddingHorizontal: spacing.lg,
      paddingVertical: spacing.sm,
    },
    buttonPressed: { opacity: 0.6 },
    buttonText: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
    },

    // Error card (spec §4).
    errorCard: {
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

    // Skeletons (spec §7).
    skeletonGroup: { gap: spacing.md },
    skeletonLine: {
      height: 16,
      borderRadius: radii.xs,
      backgroundColor: p.raised,
    },
    skeletonCard: {
      height: 120,
      borderRadius: radii.lg,
      backgroundColor: p.raised,
    },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };
