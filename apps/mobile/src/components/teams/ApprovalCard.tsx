import Feather from "@expo/vector-icons/Feather";
import { useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import {
  approvalLabel,
  approvalRequesterLabel,
  approvalStatusMeta,
  isApprovalActionable,
  timeAgo,
  type CompanyMember,
  type TeamsAgent,
  type TeamsApproval,
} from "../../lib/teamsData";
import { ReasonSheet } from "./ReasonSheet";

// ---------------------------------------------------------------------------
// Approval card (Teams v3 T3b). Rows carry no title — the label is synthesized
// from `type` + `payload` (`approvalLabel`). Shows the requester name (resolved
// via the shared agent/member resolvers), relative time, a status chip, and —
// while actionable — inline Approve / Reject actions. Tapping the card body
// (not the buttons) navigates to the linked issue when the approval has one.
// Reject collects its required reason via the cross-platform ReasonSheet
// (the old iOS-only Alert.prompt dead-ended Android).
// ---------------------------------------------------------------------------

export interface ApprovalCardProps {
  approval: TeamsApproval;
  agents: TeamsAgent[];
  members: CompanyMember[];
  onApprove: (id: string) => void;
  onReject: (id: string, reason: string) => void;
  /** Navigate to the linked issue when the card body is tapped (if any). */
  onOpenIssue?: (issueId: string) => void;
}

export function ApprovalCard({
  approval,
  agents,
  members,
  onApprove,
  onReject,
  onOpenIssue,
}: ApprovalCardProps) {
  const [rejectOpen, setRejectOpen] = useState(false);
  const label = approvalLabel(approval);
  const requester = approvalRequesterLabel(approval, agents, members);
  const when = timeAgo(approval.createdAt);
  const meta = [requester, when].filter(Boolean).join(" · ");
  const actionable = isApprovalActionable(approval);
  const statusMeta = approvalStatusMeta(approval.status);
  const linked = approval.issueId != null && approval.issueId !== "";
  const canOpen = linked && onOpenIssue != null;

  const Body = (
    <View style={styles.head}>
      <Feather
        name="shield"
        size={16}
        color={colors.forest}
        style={styles.headIcon}
      />
      <View style={styles.headText}>
        <Text style={styles.label} numberOfLines={2}>
          {label}
        </Text>
        {meta !== "" && (
          <Text style={styles.meta} numberOfLines={1}>
            {meta}
          </Text>
        )}
      </View>
      {canOpen ? (
        <Feather name="chevron-right" size={16} color={colors.muteSoft} />
      ) : null}
    </View>
  );

  return (
    <View style={styles.card}>
      {canOpen ? (
        <Pressable
          onPress={() => onOpenIssue?.(approval.issueId as string)}
          style={({ pressed }) => [styles.bodyPress, pressed && styles.bodyPressed]}
          accessibilityRole="button"
          accessibilityLabel={`${label}. ${meta}. Open linked issue.`}
        >
          {Body}
        </Pressable>
      ) : (
        <View style={styles.bodyPress}>{Body}</View>
      )}

      <View style={styles.footer}>
        <View style={[styles.statusChip, { backgroundColor: statusMeta.tint }]}>
          <Text style={[styles.statusText, { color: statusMeta.color }]} numberOfLines={1}>
            {statusMeta.label}
          </Text>
        </View>
        {actionable ? (
          <View style={styles.actions}>
            <Pressable
              onPress={() => onApprove(approval.id)}
              style={({ pressed }) => [
                styles.actionBtn,
                styles.approveBtn,
                pressed && styles.actionPressed,
              ]}
              accessibilityRole="button"
              accessibilityLabel={`Approve ${label}`}
            >
              <Feather name="check" size={14} color={colors.white} />
              <Text style={styles.approveText}>Approve</Text>
            </Pressable>
            <Pressable
              onPress={() => setRejectOpen(true)}
              style={({ pressed }) => [
                styles.actionBtn,
                styles.rejectBtn,
                pressed && styles.actionPressed,
              ]}
              accessibilityRole="button"
              accessibilityLabel={`Reject ${label}`}
            >
              <Feather name="x" size={14} color={colors.errorText} />
              <Text style={styles.rejectText}>Reject</Text>
            </Pressable>
          </View>
        ) : null}
      </View>

      <ReasonSheet
        visible={rejectOpen}
        title="Reject approval"
        placeholder="Why are you rejecting this request?"
        onSubmit={(reason) => onReject(approval.id, reason)}
        onClose={() => setRejectOpen(false)}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.md,
    gap: spacing.md,
  },
  bodyPress: { borderRadius: radii.md },
  bodyPressed: { opacity: 0.6 },
  head: { flexDirection: "row", alignItems: "flex-start", gap: spacing.sm },
  headIcon: { marginTop: 1 },
  headText: { flex: 1, gap: 2 },
  label: {
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 19,
    color: colors.ink,
  },
  meta: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },

  footer: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: spacing.sm,
  },
  statusChip: {
    paddingHorizontal: spacing.sm,
    paddingVertical: 3,
    borderRadius: radii.sm,
  },
  statusText: {
    fontFamily: fonts.sansMedium,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 0.3,
  },

  actions: { flexDirection: "row", gap: spacing.sm },
  actionBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    borderRadius: radii.sm,
    borderWidth: 1,
  },
  actionPressed: { opacity: 0.6 },
  approveBtn: { backgroundColor: colors.forest, borderColor: colors.forest },
  approveText: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.white,
  },
  rejectBtn: { backgroundColor: colors.white, borderColor: colors.errorBorder },
  rejectText: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.errorText,
  },
});
