// apps/frontend/src/components/teams/issues/lib/interaction-audience.ts

// Ported from upstream Paperclip's interaction-audience
// (paperclip/ui/src/lib/interaction-audience.ts @ v2026.1012.0-isol8.4)
// (MIT, (c) 2025 Paperclip AI). Precedence, labels, summaries, short summaries
// and narrowing notes are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - @paperclipai/shared types -> components/teams/shared/types
//   - describeInteractionAudience returns null when the row carries no
//     effectiveResolverPolicy (an older Paperclip omits the audience fields),
//     so the card renders nothing instead of guessing; a missing requested
//     policy falls back to the effective one
//   - not ported: the creation-surface helpers (RESOLVER_POLICY_CHOICES,
//     resolverPolicyLabel / resolverPolicyEffect, DEFAULT_RESOLVER_POLICY) —
//     no Teams surface creates interactions or edits audience defaults

/**
 * Plain-language presentation of *who may resolve* an issue-thread interaction.
 *
 * The product default is open: an interaction created without an explicit
 * `resolverPolicy` is resolvable by `anyone` in the company. `not_creator`,
 * `human_only`, and a named addressee are *narrowing* controls.
 *
 * Presentation only: the server evaluator owns authority, and every resolution
 * route re-checks it. Everything here derives from the server snapshot fields.
 */

import type {
  AttentionItem,
  IssueThreadInteraction,
  IssueThreadInteractionCanonicalResolverPolicy,
  IssueThreadInteractionEffectiveResolverPolicySource,
  IssueThreadInteractionResolverPolicyProvenance,
} from "@/components/teams/shared/types";

/** Short label for a resolver audience. */
const RESOLVER_POLICY_LABELS: Record<IssueThreadInteractionCanonicalResolverPolicy, string> = {
  anyone: "Anyone",
  not_creator: "Anyone except creator",
  human_only: "Human only",
};

/** Why an effective audience ended up narrower than the requested one. */
export type InteractionAudienceNarrowing =
  | "requested"
  | "company_cap"
  | "governed_action"
  | "addressee"
  | "legacy_restriction";

export interface InteractionAudienceDescription {
  /** Effective canonical policy the server will enforce. */
  policy: IssueThreadInteractionCanonicalResolverPolicy;
  /** Canonical policy the creator asked for (before caps/clamps). */
  requestedPolicy: IssueThreadInteractionCanonicalResolverPolicy;
  /** Short badge label for the effective audience. */
  label: string;
  /** One sentence naming exactly who can respond to *this* card. */
  summary: string;
  /** The same fact in a glanceable clause, for collapsed attention rows. */
  shortSummary: string;
  /** True when the card is open to the whole company with no addressee. */
  isOpen: boolean;
  /** Set when something narrows the audience below the open default. */
  narrowedBy: InteractionAudienceNarrowing | null;
  /** Extra sentence explaining a narrowing the requester did not ask for. */
  narrowedNote: string | null;
}

/** The server-evaluated facts an audience description is derived from. */
export interface InteractionAudienceFacts {
  effectiveResolverPolicy: IssueThreadInteractionCanonicalResolverPolicy;
  requestedResolverPolicy: IssueThreadInteractionCanonicalResolverPolicy;
  effectiveResolverPolicySource?: IssueThreadInteractionEffectiveResolverPolicySource;
  resolverPolicyProvenance?: IssueThreadInteractionResolverPolicyProvenance;
  hasAddressee: boolean;
  isUserAddressee?: boolean;
}

/**
 * Describe the effective audience of one interaction. Precedence mirrors the
 * server evaluator's narrowing order: `human_only` beats a named addressee,
 * which beats `not_creator`, which beats the open default.
 */
export function describeInteractionAudience({
  interaction,
  creatorLabel,
  addresseeLabel,
}: {
  interaction: IssueThreadInteraction;
  creatorLabel?: string | null;
  addresseeLabel?: string | null;
}): InteractionAudienceDescription | null {
  const effective = interaction.effectiveResolverPolicy;
  if (!effective) return null;
  return describeResolverAudience({
    facts: {
      effectiveResolverPolicy: effective,
      requestedResolverPolicy: interaction.requestedResolverPolicy ?? effective,
      effectiveResolverPolicySource: interaction.effectiveResolverPolicySource,
      resolverPolicyProvenance: interaction.resolverPolicyProvenance,
      hasAddressee: Boolean(interaction.addresseeAgentId || interaction.addresseeUserId),
      isUserAddressee: Boolean(interaction.addresseeUserId),
    },
    creatorLabel,
    addresseeLabel,
  });
}

/** Describe an effective audience from the server-evaluated facts alone. */
export function describeResolverAudience({
  facts,
  creatorLabel,
  addresseeLabel,
}: {
  facts: InteractionAudienceFacts;
  creatorLabel?: string | null;
  addresseeLabel?: string | null;
}): InteractionAudienceDescription {
  const policy = facts.effectiveResolverPolicy;
  const requestedPolicy = facts.requestedResolverPolicy;
  const hasAddressee = facts.hasAddressee;
  const isUserAddressee = facts.isUserAddressee === true;
  // "You" is right for a badge and wrong mid-sentence ("Only You can respond.").
  const midSentence = (label: string) => (label === "You" ? "you" : label);
  const addressee = midSentence(
    addresseeLabel?.trim() || (isUserAddressee ? "the addressed user" : "the addressed agent"),
  );
  const creator = midSentence(creatorLabel?.trim() || "the agent that created it");

  const summary = isUserAddressee
    ? `Only ${addressee} can respond.`
    : policy === "human_only"
    ? `${hasAddressee ? `Assigned to ${addressee}. ` : ""}Only a person on the board can respond — agents cannot resolve this card.`
    : hasAddressee
      ? `Only ${addressee} or a person on the board can respond.`
      : policy === "not_creator"
        ? `Anyone in the organization except ${creator} can respond.`
        : "Anyone in the organization can respond — the board or any agent, including the one that asked.";

  const shortSummary = isUserAddressee
    ? `Only ${addressee} can respond`
    : policy === "human_only"
    ? hasAddressee
      ? `Assigned to ${addressee} · board only`
      : "Only the board can respond"
    : hasAddressee
      ? `Only ${addressee} or the board can respond`
      : policy === "not_creator"
        ? `Anyone except ${creator} can respond`
        : "Anyone can respond";

  const source = facts.effectiveResolverPolicySource;
  const provenance = facts.resolverPolicyProvenance;

  // Only narrowing the requester did *not* ask for earns an extra sentence.
  const narrowedNote = source === "governed_action"
    ? "This card runs a governed action, so it stays human-only whatever audience was requested."
    : source === "company_cap"
      ? `Organization interaction governance narrowed this from ${RESOLVER_POLICY_LABELS[requestedPolicy]} to ${RESOLVER_POLICY_LABELS[policy]}.`
      : provenance === "legacy_inherited_restriction"
        ? "Created before Anyone became the default, so it stays restricted. A new card would be open."
        : null;

  const narrowedBy: InteractionAudienceNarrowing | null = source === "governed_action"
    ? "governed_action"
    : source === "company_cap"
      ? "company_cap"
      : provenance === "legacy_inherited_restriction"
        ? "legacy_restriction"
        : policy !== "anyone"
          ? "requested"
          : hasAddressee
            ? "addressee"
            : null;

  return {
    policy,
    requestedPolicy,
    // A named addressee owns the response, so the label must not read "Anyone"
    // while the sentence names one actor.
    label: (policy !== "human_only" || isUserAddressee) && hasAddressee
      ? "Addressed"
      : RESOLVER_POLICY_LABELS[policy],
    summary,
    shortSummary,
    isOpen: policy === "anyone" && !hasAddressee,
    narrowedBy,
    narrowedNote,
  };
}

/**
 * Audience of an `issue_thread_interaction` attention row, from the metadata the
 * feed ships with the item. Null for every other source kind and for a feed
 * built before the metadata existed.
 */
export function describeAttentionResolverAudience(
  item: Pick<AttentionItem, "sourceKind" | "resolverAudience">,
): InteractionAudienceDescription | null {
  if (item.sourceKind !== "issue_thread_interaction") return null;
  const audience = item.resolverAudience;
  if (!audience) return null;
  return describeResolverAudience({
    facts: {
      effectiveResolverPolicy: audience.effectiveResolverPolicy,
      requestedResolverPolicy: audience.requestedResolverPolicy,
      effectiveResolverPolicySource: audience.effectiveResolverPolicySource,
      resolverPolicyProvenance: audience.resolverPolicyProvenance,
      hasAddressee: Boolean(audience.addresseeAgentId || audience.addresseeUserId),
      isUserAddressee: Boolean(audience.addresseeUserId),
    },
    creatorLabel: audience.createdByAgentName,
    addresseeLabel: audience.addresseeName,
  });
}
