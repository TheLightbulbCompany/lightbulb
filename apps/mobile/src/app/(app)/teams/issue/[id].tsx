import { Stack, useLocalSearchParams } from "expo-router";
import { type ReactElement } from "react";
import {
  Animated,
  Linking,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { MessageMarkdown } from "../../../../components/chat/MessageMarkdown";
import { usePulse } from "../../../../components/chat/usePulse";
import { colors, fonts, radii, spacing, type } from "../../../../config/theme";
import {
  assigneeLabel,
  commentAuthorLabel,
  humanizeStatus,
  looksLikeMarkdown,
  timeAgo,
  useTeamsIssue,
  type ChildIssue,
  type IssueComment,
  type IssueDetail,
  type TeamsAgent,
} from "../../../../lib/teamsData";

const MANAGE_URL = "https://isol8.co/teams";

// ---------------------------------------------------------------------------
// Loading / readiness affordances
// ---------------------------------------------------------------------------

function PulsingDot() {
  const opacity = usePulse(true, 0.3);
  return <Animated.View style={[styles.pulseDot, { opacity }]} />;
}

function SkeletonBody() {
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
  if (looksLikeMarkdown(text)) {
    return <MessageMarkdown text={text} />;
  }
  return <Text style={styles.plainBody}>{text}</Text>;
}

function PropertyRow({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.propRow}>
      <Text style={styles.propLabel}>{label}</Text>
      <Text style={styles.propValue} numberOfLines={2}>
        {value}
      </Text>
    </View>
  );
}

function CommentRow({
  comment,
  agents,
}: {
  comment: IssueComment;
  agents: TeamsAgent[];
}) {
  return (
    <View style={styles.comment}>
      <View style={styles.commentMeta}>
        <Text style={styles.commentAuthor}>
          {commentAuthorLabel(comment, agents)}
        </Text>
        <Text style={styles.commentDot}>·</Text>
        <Text style={styles.commentTime}>{timeAgo(comment.createdAt)}</Text>
      </View>
      <BodyText text={comment.body} />
    </View>
  );
}

function ChildRow({ child }: { child: ChildIssue }) {
  return (
    <View style={styles.childRow}>
      <Text style={styles.childIdent}>
        {child.identifier ?? child.id.slice(0, 8)}
      </Text>
      <Text style={styles.childTitle} numberOfLines={1}>
        {child.title}
      </Text>
      <Text style={styles.childStatus}>{humanizeStatus(child.status)}</Text>
    </View>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function IssueDetailScreen() {
  const params = useLocalSearchParams<{ id: string; title?: string }>();
  const id = typeof params.id === "string" ? params.id : undefined;
  const seedTitle = typeof params.title === "string" ? params.title : undefined;
  const { state, retry } = useTeamsIssue(id);

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
      body = (
        <CenteredState
          title="Teams requires an active subscription"
          detail="Manage your subscription on isol8.co to unlock your team workspace."
          action={
            <OutlineButton
              label="Manage on isol8.co"
              onPress={() => void Linking.openURL(MANAGE_URL)}
            />
          }
        />
      );
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
    body = <ReadyBody state={state} />;
  }

  return (
    <>
      <Stack.Screen options={{ title: headerTitle }} />
      <ScrollView style={styles.page} contentContainerStyle={styles.content}>
        {body}
      </ScrollView>
    </>
  );
}

function ReadyBody({
  state,
}: {
  state: {
    issue: IssueDetail;
    comments: IssueComment[];
    children: ChildIssue[];
    agents: TeamsAgent[];
  };
}) {
  const { issue, comments, children, agents } = state;
  const identifier = issue.identifier ?? issue.id.slice(0, 8);
  const assignee = assigneeLabel(issue, agents);
  const created = issue.createdAt ? timeAgo(issue.createdAt) : null;

  return (
    <>
      {/* Title + identifier */}
      <View style={styles.headerBlock}>
        <Text style={styles.identifier}>{identifier}</Text>
        <Text style={styles.issueTitle}>{issue.title}</Text>
      </View>

      {/* Properties */}
      <View style={styles.card}>
        <PropertyRow label="STATUS" value={humanizeStatus(issue.status)} />
        {assignee != null && <PropertyRow label="AGENT" value={assignee} />}
        {issue.priority != null && (
          <PropertyRow label="PRIORITY" value={humanizeStatus(issue.priority)} />
        )}
        {created != null && <PropertyRow label="CREATED" value={created} />}
      </View>

      {/* Description */}
      {issue.description != null && issue.description.trim() !== "" && (
        <View style={styles.section}>
          <Text style={styles.sectionLabel}>DESCRIPTION</Text>
          <BodyText text={issue.description} />
        </View>
      )}

      {/* Sub-issues summary (cheap children read) */}
      {children.length > 0 && (
        <View style={styles.section}>
          <Text style={styles.sectionLabel}>
            SUB-ISSUES
            <Text style={styles.sectionCount}> · {children.length}</Text>
          </Text>
          <View style={styles.card}>
            {children.map((child) => (
              <ChildRow key={child.id} child={child} />
            ))}
          </View>
        </View>
      )}

      {/* Comments thread */}
      <View style={styles.section}>
        <Text style={styles.sectionLabel}>
          COMMENTS
          {comments.length > 0 && (
            <Text style={styles.sectionCount}> · {comments.length}</Text>
          )}
        </Text>
        {comments.length === 0 ? (
          <Text style={styles.emptyComments}>No comments yet.</Text>
        ) : (
          <View style={styles.commentList}>
            {comments.map((comment) => (
              <CommentRow key={comment.id} comment={comment} agents={agents} />
            ))}
          </View>
        )}
      </View>

      {/* Read-only note (v1: no posting comments). */}
      <Text style={styles.readOnlyNote}>
        Viewing is read-only in the mobile app. Reply from isol8.co.
      </Text>
    </>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  content: { padding: spacing.lg, gap: spacing.lg, paddingBottom: spacing.xl },

  // Header block.
  headerBlock: { gap: spacing.xs },
  identifier: {
    fontFamily: fonts.mono,
    fontSize: 11,
    letterSpacing: 1,
    color: colors.mute,
  },
  issueTitle: { ...type.title, fontSize: 18, lineHeight: 24 },

  // Property card.
  card: {
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.lg,
    gap: spacing.md,
  },
  propRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
  },
  propLabel: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1.4,
    color: colors.mute,
    width: 76,
  },
  propValue: {
    flex: 1,
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },

  // Sections.
  section: { gap: spacing.sm },
  sectionLabel: { ...type.overline },
  sectionCount: { color: colors.sectionCount },

  plainBody: {
    fontFamily: fonts.dmSans,
    fontSize: 14,
    lineHeight: 22,
    color: colors.assistantInk,
  },

  // Sub-issue rows.
  childRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
  },
  childIdent: {
    fontFamily: fonts.mono,
    fontSize: 10,
    letterSpacing: 0.6,
    color: colors.mute,
  },
  childTitle: {
    flex: 1,
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },
  childStatus: {
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },

  // Comments.
  commentList: { gap: spacing.lg },
  comment: {
    gap: spacing.xs,
    paddingLeft: spacing.md,
    borderLeftWidth: 2,
    borderLeftColor: colors.edge,
  },
  commentMeta: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
  },
  commentAuthor: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.inkSoft,
  },
  commentDot: { fontFamily: fonts.dmSans, fontSize: 12, color: colors.mute },
  commentTime: {
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 16,
    color: colors.mute,
  },
  emptyComments: {
    fontFamily: fonts.lora,
    fontSize: 14,
    lineHeight: 20,
    color: colors.mute,
    fontStyle: "italic",
  },

  readOnlyNote: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.muteSoft,
    textAlign: "center",
    marginTop: spacing.sm,
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
    backgroundColor: colors.forestSoft,
  },
  stateTitle: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 15,
    lineHeight: 20,
    color: colors.ink,
    textAlign: "center",
  },
  stateDetail: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 19,
    color: colors.mute,
    textAlign: "center",
  },
  button: {
    marginTop: spacing.xs,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.sm,
    backgroundColor: colors.white,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
  },
  buttonPressed: { opacity: 0.6 },
  buttonText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.forest,
  },

  // Error card (spec §4).
  errorCard: {
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

  // Skeletons (spec §7).
  skeletonGroup: { gap: spacing.md },
  skeletonLine: {
    height: 16,
    borderRadius: radii.xs,
    backgroundColor: colors.skeleton,
  },
  skeletonCard: {
    height: 120,
    borderRadius: radii.lg,
    backgroundColor: colors.skeleton,
  },
});
