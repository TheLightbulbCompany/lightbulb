import Feather from "@expo/vector-icons/Feather";
import { useMemo, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { MessageMarkdown } from "../chat/MessageMarkdown";
import {
  timeAgo,
  type InteractionAnswer,
  type IssueInteraction,
  type TeamsAgent,
} from "../../lib/teamsData";
import { ReasonSheet } from "./ReasonSheet";

// ---------------------------------------------------------------------------
// Thread-interaction card (lean port of web IssueThreadInteractionCard): an
// agent-initiated request that blocks its run until the human answers.
// Four kinds — request_confirmation (Accept/Decline), request_checkbox_
// confirmation (checkbox list → Accept), ask_user_questions (per-question
// options → Submit), suggest_tasks (task list w/ checkboxes → Accept).
// Pending shows the action UI; resolved collapses to a summary line.
// ---------------------------------------------------------------------------

function summaryLine(interaction: IssueInteraction): string {
  const { kind, status, payload, result } = interaction;
  if (kind === "suggest_tasks") {
    const count = payload?.tasks?.length ?? 0;
    if (status === "accepted") {
      const created = result?.createdTasks?.length ?? 0;
      const skipped = result?.skippedClientKeys?.length ?? 0;
      return skipped > 0
        ? `Accepted ${created} of ${count} tasks`
        : `Accepted ${created} task${created === 1 ? "" : "s"}`;
    }
    if (status === "rejected") return `Rejected ${count} task${count === 1 ? "" : "s"}`;
    return `Suggested ${count} task${count === 1 ? "" : "s"}`;
  }
  if (kind === "request_confirmation") {
    if (status === "accepted") return "Confirmed request";
    if (status === "rejected") return "Declined request";
    if (status === "expired") return "Confirmation expired";
    return "Requested confirmation";
  }
  if (kind === "request_checkbox_confirmation") {
    const optionCount = payload?.options?.length ?? 0;
    if (status === "accepted") {
      const selected = result?.selectedOptionIds?.length ?? 0;
      return `Confirmed ${selected} of ${optionCount} options`;
    }
    if (status === "rejected") return "Declined selection";
    if (status === "expired") return "Selection expired";
    return `Requested a selection from ${optionCount} option${optionCount === 1 ? "" : "s"}`;
  }
  const count = payload?.questions?.length ?? 0;
  if (status === "answered") return `Answered ${count} question${count === 1 ? "" : "s"}`;
  if (status === "cancelled") return "Cancelled";
  return `Asked ${count} question${count === 1 ? "" : "s"}`;
}

function Checkbox({ checked }: { checked: boolean }) {
  return (
    <View style={[styles.checkbox, checked && styles.checkboxChecked]}>
      {checked ? <Feather name="check" size={12} color={colors.white} /> : null}
    </View>
  );
}

export function InteractionCard({
  interaction,
  agents,
  onAccept,
  onReject,
  onRespond,
}: {
  interaction: IssueInteraction;
  agents: TeamsAgent[];
  onAccept: (
    id: string,
    selection?: { selectedClientKeys?: string[]; selectedOptionIds?: string[] },
  ) => void;
  onReject: (id: string, reason?: string) => void;
  onRespond: (id: string, answers: InteractionAnswer[]) => void;
}) {
  const { kind, payload } = interaction;
  const pending = interaction.status === "pending";
  const agentName =
    agents.find((a) => a.id === interaction.createdByAgentId)?.name ?? "Agent";
  const [rejectOpen, setRejectOpen] = useState(false);

  // Checkbox-confirmation + suggested-task selection state, seeded from the
  // payload defaults / all options (web card parity) — capped at maxSelected
  // so a bounded selection never mounts already over its own limit.
  const [selectedOptions, setSelectedOptions] = useState<Set<string>>(() => {
    const seed =
      payload?.defaultSelectedOptionIds ?? payload?.options?.map((o) => o.id) ?? [];
    const max = payload?.maxSelected;
    return new Set(max != null ? seed.slice(0, max) : seed);
  });
  const [selectedTasks, setSelectedTasks] = useState<Set<string>>(
    () => new Set(payload?.tasks?.map((t) => t.clientKey) ?? []),
  );
  // ask_user_questions answers: questionId → selected option ids.
  const [answers, setAnswers] = useState<Map<string, Set<string>>>(() => new Map());

  const questions = useMemo(() => payload?.questions ?? [], [payload?.questions]);
  const answeredAll = questions.every((q) => {
    if (q.required === false) return true;
    return (answers.get(q.id)?.size ?? 0) > 0;
  });

  // Checkbox-confirmation selection bounds (web card parity): unchecked rows
  // lock at maxSelected, Accept gates on the min/max window, and the accept
  // body is capped belt-and-braces.
  const minSelected = payload?.minSelected ?? 0;
  const maxSelected = payload?.maxSelected ?? null;
  const selectedCount = selectedOptions.size;
  const atMax = maxSelected != null && selectedCount >= maxSelected;
  const belowMin = selectedCount < minSelected;
  const aboveMax = maxSelected != null && selectedCount > maxSelected;
  const selectionValid = !belowMin && !aboveMax;
  const selectionHint = belowMin
    ? `Select at least ${minSelected} option${minSelected === 1 ? "" : "s"}.`
    : aboveMax && maxSelected != null
      ? `Select at most ${maxSelected} option${maxSelected === 1 ? "" : "s"}.`
      : null;

  const toggle = (set: Set<string>, id: string): Set<string> => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  };

  const title =
    interaction.title ??
    payload?.title ??
    payload?.prompt ??
    summaryLine(interaction);

  return (
    <View style={styles.card}>
      <View style={styles.head}>
        <Feather name="help-circle" size={16} color={colors.forest} style={styles.headIcon} />
        <View style={styles.headText}>
          <Text style={styles.title}>{title}</Text>
          <Text style={styles.meta} numberOfLines={1}>
            {[agentName, timeAgo(interaction.createdAt)].filter(Boolean).join(" · ")}
          </Text>
        </View>
      </View>

      {payload?.detailsMarkdown ? (
        <MessageMarkdown text={payload.detailsMarkdown} />
      ) : null}

      {!pending ? (
        <View style={styles.resolvedRow}>
          <Feather
            name={
              interaction.status === "accepted" || interaction.status === "answered"
                ? "check-circle"
                : "x-circle"
            }
            size={13}
            color={
              interaction.status === "accepted" || interaction.status === "answered"
                ? colors.forest
                : colors.mute
            }
          />
          <Text style={styles.resolvedText}>{summaryLine(interaction)}</Text>
        </View>
      ) : (
        <>
          {kind === "request_checkbox_confirmation" && (
            <View style={styles.optionList}>
              {(payload?.options ?? []).map((option) => {
                const isSelected = selectedOptions.has(option.id);
                // Unchecked rows lock once the selection hits maxSelected —
                // unchecking always stays available.
                const locked = !isSelected && atMax;
                return (
                  <Pressable
                    key={option.id}
                    style={[styles.optionRow, locked && styles.optionLocked]}
                    onPress={() => {
                      if (locked) return;
                      setSelectedOptions((prev) => toggle(prev, option.id));
                    }}
                    disabled={locked}
                    accessibilityRole="checkbox"
                    accessibilityState={{ checked: isSelected, disabled: locked }}
                    accessibilityLabel={option.label}
                  >
                    <Checkbox checked={isSelected} />
                    <View style={styles.optionText}>
                      <Text style={styles.optionLabel}>{option.label}</Text>
                      {option.description ? (
                        <Text style={styles.optionDetail}>{option.description}</Text>
                      ) : null}
                    </View>
                  </Pressable>
                );
              })}
              {selectionHint ? (
                <Text style={styles.selectionHint}>{selectionHint}</Text>
              ) : null}
            </View>
          )}

          {kind === "suggest_tasks" && (
            <View style={styles.optionList}>
              {(payload?.tasks ?? []).map((task) => (
                <Pressable
                  key={task.clientKey}
                  style={styles.optionRow}
                  onPress={() => setSelectedTasks((prev) => toggle(prev, task.clientKey))}
                  accessibilityRole="checkbox"
                  accessibilityState={{ checked: selectedTasks.has(task.clientKey) }}
                  accessibilityLabel={task.title}
                >
                  <Checkbox checked={selectedTasks.has(task.clientKey)} />
                  <View style={styles.optionText}>
                    <Text style={styles.optionLabel}>{task.title}</Text>
                    {task.description ? (
                      <Text style={styles.optionDetail} numberOfLines={2}>
                        {task.description}
                      </Text>
                    ) : null}
                  </View>
                </Pressable>
              ))}
            </View>
          )}

          {kind === "ask_user_questions" &&
            questions.map((question) => {
              const selected = answers.get(question.id) ?? new Set<string>();
              return (
                <View key={question.id} style={styles.questionBlock}>
                  <Text style={styles.questionPrompt}>{question.prompt}</Text>
                  {question.helpText ? (
                    <Text style={styles.optionDetail}>{question.helpText}</Text>
                  ) : null}
                  {(question.options ?? []).map((option) => {
                    const isSelected = selected.has(option.id);
                    return (
                      <Pressable
                        key={option.id}
                        style={styles.optionRow}
                        onPress={() =>
                          setAnswers((prev) => {
                            const next = new Map(prev);
                            if (question.selectionMode === "single") {
                              next.set(question.id, isSelected ? new Set() : new Set([option.id]));
                            } else {
                              next.set(question.id, toggle(selected, option.id));
                            }
                            return next;
                          })
                        }
                        accessibilityRole={
                          question.selectionMode === "single" ? "radio" : "checkbox"
                        }
                        accessibilityState={{ checked: isSelected }}
                        accessibilityLabel={option.label}
                      >
                        <Checkbox checked={isSelected} />
                        <View style={styles.optionText}>
                          <Text style={styles.optionLabel}>{option.label}</Text>
                          {option.description ? (
                            <Text style={styles.optionDetail}>{option.description}</Text>
                          ) : null}
                        </View>
                      </Pressable>
                    );
                  })}
                </View>
              );
            })}

          <View style={styles.actions}>
            {kind === "ask_user_questions" ? (
              <Pressable
                style={({ pressed }) => [
                  styles.acceptBtn,
                  !answeredAll && styles.btnDisabled,
                  pressed && styles.pressed,
                ]}
                disabled={!answeredAll}
                onPress={() =>
                  onRespond(
                    interaction.id,
                    questions.map((q) => ({
                      questionId: q.id,
                      optionIds: [...(answers.get(q.id) ?? [])],
                    })),
                  )
                }
                accessibilityRole="button"
                accessibilityLabel={payload?.submitLabel ?? "Submit answers"}
              >
                <Text style={styles.acceptText}>{payload?.submitLabel ?? "Submit"}</Text>
              </Pressable>
            ) : (
              (() => {
                const acceptBlocked =
                  kind === "request_checkbox_confirmation" && !selectionValid;
                return (
                  <Pressable
                    style={({ pressed }) => [
                      styles.acceptBtn,
                      acceptBlocked && styles.btnDisabled,
                      pressed && styles.pressed,
                    ]}
                    disabled={acceptBlocked}
                    onPress={() =>
                      onAccept(
                        interaction.id,
                        kind === "request_checkbox_confirmation"
                          ? {
                              // Belt-and-braces cap alongside the toggle lock
                              // (web parity: slice at maxSelected).
                              selectedOptionIds:
                                maxSelected != null
                                  ? [...selectedOptions].slice(0, maxSelected)
                                  : [...selectedOptions],
                            }
                          : kind === "suggest_tasks"
                            ? { selectedClientKeys: [...selectedTasks] }
                            : undefined,
                      )
                    }
                    accessibilityRole="button"
                    accessibilityLabel={payload?.acceptLabel ?? "Accept"}
                  >
                    <Feather name="check" size={14} color={colors.white} />
                    <Text style={styles.acceptText}>
                      {payload?.acceptLabel ?? "Accept"}
                    </Text>
                  </Pressable>
                );
              })()
            )}
            <Pressable
              style={({ pressed }) => [styles.rejectBtn, pressed && styles.pressed]}
              onPress={() => {
                if (payload?.rejectRequiresReason) setRejectOpen(true);
                else onReject(interaction.id);
              }}
              accessibilityRole="button"
              accessibilityLabel={payload?.rejectLabel ?? "Decline"}
            >
              <Text style={styles.rejectText}>{payload?.rejectLabel ?? "Decline"}</Text>
            </Pressable>
          </View>
        </>
      )}

      <ReasonSheet
        visible={rejectOpen}
        title="Decline request"
        placeholder="Why are you declining?"
        submitLabel="Decline"
        onSubmit={(reason) => onReject(interaction.id, reason)}
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
    borderColor: colors.forestFocusBorder,
    padding: spacing.md,
    gap: spacing.md,
  },
  head: { flexDirection: "row", alignItems: "flex-start", gap: spacing.sm },
  headIcon: { marginTop: 1 },
  headText: { flex: 1, gap: 2 },
  title: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 14,
    lineHeight: 19,
    color: colors.ink,
  },
  meta: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },

  resolvedRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  resolvedText: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },

  optionList: { gap: spacing.sm },
  optionRow: { flexDirection: "row", alignItems: "flex-start", gap: spacing.sm },
  optionText: { flex: 1, gap: 2 },
  optionLabel: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },
  optionDetail: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },
  checkbox: {
    width: 18,
    height: 18,
    borderRadius: radii.xs,
    borderWidth: 1.5,
    borderColor: colors.edge,
    alignItems: "center",
    justifyContent: "center",
    marginTop: 1,
  },
  checkboxChecked: { backgroundColor: colors.forest, borderColor: colors.forest },

  optionLocked: { opacity: 0.45 },
  selectionHint: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.warn,
  },

  questionBlock: { gap: spacing.sm },
  questionPrompt: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },

  actions: { flexDirection: "row", gap: spacing.sm },
  pressed: { opacity: 0.6 },
  btnDisabled: { backgroundColor: colors.creamDeepest, borderColor: colors.creamDeepest },
  acceptBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    borderRadius: radii.sm,
    borderWidth: 1,
    backgroundColor: colors.forest,
    borderColor: colors.forest,
  },
  acceptText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.white,
  },
  rejectBtn: {
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    borderRadius: radii.sm,
    borderWidth: 1,
    backgroundColor: colors.white,
    borderColor: colors.edge,
  },
  rejectText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },
});
