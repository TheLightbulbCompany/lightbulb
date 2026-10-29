import { Image } from "expo-image";
import { router } from "expo-router";
import { SymbolView, type SFSymbol } from "../ui/SymbolView";
import { useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  useColorScheme,
  View,
} from "react-native";

import { chatPalette, fonts, radii, spacing, type ChatPalette } from "../../config/theme";
import { actorName } from "../../lib/actorName";
import { MessageMarkdown } from "../chat/MessageMarkdown";
import { CardReplyContext, readOnlyCardReply } from "../chat/openui/CardReplyContext";
import { DocumentEditSheet } from "./DocumentEditSheet";
import {
  fetchAttachmentDataUri,
  timeAgo,
  useTeamsConfig,
  type IssueAttachment,
  type IssueDocument,
  type CompanyMember,
  type IssueWorkProduct,
  type TeamsAgent,
} from "../../lib/teamsData";

// ---------------------------------------------------------------------------
// Issue outputs — the "what did the agent produce" read surfaces (web
// IssueWorkProductsSection / IssueDocumentsSection / IssueAttachmentsSection
// parity, read-first):
//   OUTPUT      typed work-product rows; Open (safe http(s) only) + the board
//               review verdict (Approve / Request changes) while
//               reviewState=needs_board_review.
//   DOCUMENTS   expandable markdown bodies.
//   ATTACHMENTS name/size rows; images preview inline via a bearer-fetched
//               data URI (expo-file-system is not installed — no native deps).
// Each section self-hides when empty.
// ---------------------------------------------------------------------------

const WORK_PRODUCT_ICON: Record<string, SFSymbol> = {
  preview_url: "globe",
  runtime_service: "server.rack",
  pull_request: "arrow.triangle.pull",
  branch: "arrow.triangle.branch",
  commit: "smallcircle.filled.circle",
  artifact: "shippingbox",
  document: "doc.text",
};

/** http(s)-only href gate (web safeHref parity). */
function safeHref(url: string | null | undefined): string | null {
  if (!url) return null;
  return /^https?:\/\//i.test(url.trim()) ? url.trim() : null;
}

function fmtBytes(bytes: number | null | undefined): string | null {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function WorkProductsSection({
  workProducts,
  onReview,
}: {
  workProducts: IssueWorkProduct[];
  onReview: (id: string, verdict: "approved" | "changes_requested") => void;
}) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  if (workProducts.length === 0) return null;
  return (
    <View style={styles.section}>
      <Text style={styles.overline}>
        OUTPUT
        <Text style={styles.sectionCount}> · {workProducts.length}</Text>
      </Text>
      {workProducts.map((wp) => {
        const href = safeHref(wp.url);
        const needsReview = wp.reviewState === "needs_board_review";
        return (
          <View key={wp.id} style={styles.card}>
            <View style={styles.rowHead}>
              <SymbolView
                accessibilityElementsHidden
                name={WORK_PRODUCT_ICON[wp.type] ?? "cube"}
                size={15}
                tintColor={palette.fg}
                style={styles.rowIcon}
              />
              <View style={styles.rowText}>
                <Text style={styles.rowTitle} numberOfLines={2}>
                  {wp.title}
                </Text>
                <Text style={styles.rowMeta} numberOfLines={1}>
                  {[wp.provider, wp.status, wp.createdAt ? timeAgo(wp.createdAt) : null]
                    .filter(Boolean)
                    .join(" · ")}
                </Text>
              </View>
              {href ? (
                <Pressable
                  onPress={() => router.push({
                    pathname: "/(app)/library",
                    params: { title: wp.title, url: href, summary: wp.summary ?? "", type: wp.type },
                  })}
                  hitSlop={6}
                  accessibilityRole="button"
                  accessibilityLabel={`Open ${wp.title}`}
                >
                  <Text style={styles.linkAction}>Open</Text>
                </Pressable>
              ) : null}
            </View>
            {wp.summary ? <MessageMarkdown text={wp.summary} palette={palette} /> : null}
            {needsReview ? (
              <View style={styles.reviewRow}>
                <Text style={styles.reviewLabel}>Needs your review</Text>
                <Pressable
                  style={({ pressed }) => [styles.approveBtn, pressed && styles.pressed]}
                  onPress={() => onReview(wp.id, "approved")}
                  accessibilityRole="button"
                  accessibilityLabel={`Approve ${wp.title}`}
                >
                  <SymbolView accessibilityElementsHidden name="checkmark" size={13} tintColor={palette.onFg} />
                  <Text style={styles.approveText}>Approve</Text>
                </Pressable>
                <Pressable
                  style={({ pressed }) => [styles.changesBtn, pressed && styles.pressed]}
                  onPress={() => onReview(wp.id, "changes_requested")}
                  accessibilityRole="button"
                  accessibilityLabel={`Request changes on ${wp.title}`}
                >
                  <Text style={styles.changesText}>Request changes</Text>
                </Pressable>
              </View>
            ) : wp.reviewState === "approved" || wp.reviewState === "changes_requested" ? (
              <Text style={styles.rowMeta}>
                {wp.reviewState === "approved" ? "Approved" : "Changes requested"}
              </Text>
            ) : null}
          </View>
        );
      })}
    </View>
  );
}

/** A document draws its cards but never answers one: no `press`, so every control is disabled. */
const READ_ONLY_CARDS = readOnlyCardReply("document");

export function DocumentsSection({
  documents,
  agents,
  members,
  currentUserId,
  onSave,
}: {
  documents: IssueDocument[];
  agents: TeamsAgent[];
  members: CompanyMember[];
  currentUserId: string | null;
  onSave: (key: string, title: string, body: string, baseRevisionId: string | null) => Promise<void>;
}) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const [openKeys, setOpenKeys] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<IssueDocument | null>(null);
  if (documents.length === 0) return null;
  return (
    <View style={styles.section}>
      <Text style={styles.overline}>
        DOCUMENTS
        <Text style={styles.sectionCount}> · {documents.length}</Text>
      </Text>
      {documents.map((doc) => {
        const open = openKeys.has(doc.key);
        return (
          <View key={doc.key} style={styles.card}>
            <Pressable
              style={styles.rowHead}
              onPress={() =>
                setOpenKeys((prev) => {
                  const next = new Set(prev);
                  if (next.has(doc.key)) next.delete(doc.key);
                  else next.add(doc.key);
                  return next;
                })
              }
              accessibilityRole="button"
              accessibilityState={{ expanded: open }}
              accessibilityLabel={doc.title ?? doc.key}
            >
              <SymbolView accessibilityElementsHidden name="doc.text" size={15} tintColor={palette.fg} style={styles.rowIcon} />
              <View style={styles.rowText}>
                <Text style={styles.rowTitle} numberOfLines={1}>
                  {doc.title ?? doc.key}
                </Text>
                {doc.updatedAt ? (
                  <Text style={styles.rowMeta}>
                    {actorName({ agentId: doc.updatedByAgentId, userId: doc.updatedByUserId, agents, members, currentUserId })} · updated{" "}
                    {timeAgo(doc.updatedAt)}
                    {doc.latestRevisionNumber != null ? ` · rev ${doc.latestRevisionNumber}` : ""}
                  </Text>
                ) : null}
              </View>
              <SymbolView
                accessibilityElementsHidden
                name={open ? "chevron.up" : "chevron.down"}
                size={15}
                tintColor={palette.fgMute}
              />
            </Pressable>
            {open ? (
              doc.body ? (
                <CardReplyContext.Provider value={READ_ONLY_CARDS}>
                  <MessageMarkdown text={doc.body} palette={palette} />
                </CardReplyContext.Provider>
              ) : (
                <Text style={styles.rowMeta}>Empty document.</Text>
              )
            ) : null}
            {open ? (
              <Pressable onPress={() => setEditing(doc)} accessibilityRole="button" accessibilityLabel={`Edit ${doc.title ?? doc.key}`}>
                <Text style={[styles.rowMeta, { color: palette.link }]}>Edit</Text>
              </Pressable>
            ) : null}
          </View>
        );
      })}
      {editing ? (
        <DocumentEditSheet
          key={editing.key}
          document={editing}
          onSave={(title, body) => onSave(editing.key, title, body, editing.latestRevisionId ?? null)}
          onClose={() => setEditing(null)}
        />
      ) : null}
    </View>
  );
}

/** Inline preview cap: only fetch data URIs for images up to 4 MB — a base64
 * data URI triples memory, and anything bigger belongs on web. */
const IMAGE_PREVIEW_MAX_BYTES = 4 * 1024 * 1024;

function AttachmentRow({ issueId, attachment }: { issueId: string; attachment: IssueAttachment }) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const config = useTeamsConfig();
  const [preview, setPreview] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const name = attachment.originalFilename ?? "attachment";
  // Fail CLOSED on an unknown size: fetchAttachmentDataUri has no cap of its
  // own, so a null byteSize must not read as "tiny".
  const isImage =
    (attachment.contentType ?? "").startsWith("image/") &&
    typeof attachment.byteSize === "number" &&
    attachment.byteSize <= IMAGE_PREVIEW_MAX_BYTES;
  const meta = [attachment.contentType, fmtBytes(attachment.byteSize)]
    .filter(Boolean)
    .join(" · ");

  return (
    <View style={styles.card}>
      <Pressable
        style={styles.rowHead}
        onPress={() => {
          if (!isImage || loading) return;
          if (preview) {
            setPreview(null);
            return;
          }
          setLoading(true);
          setFailed(false);
          void fetchAttachmentDataUri(config, issueId, attachment.id).then((uri) => {
            setLoading(false);
            if (uri) setPreview(uri);
            else setFailed(true);
          });
        }}
        accessibilityRole={isImage ? "button" : "none"}
        accessibilityLabel={isImage ? `Preview ${name}` : name}
      >
        <SymbolView
          accessibilityElementsHidden
          name={isImage ? "photo" : "paperclip"}
          size={15}
          tintColor={palette.fg}
          style={styles.rowIcon}
        />
        <View style={styles.rowText}>
          <Text style={styles.rowTitle} numberOfLines={1}>
            {name}
          </Text>
          {meta !== "" && <Text style={styles.rowMeta}>{meta}</Text>}
        </View>
        {loading ? (
          <ActivityIndicator size="small" color={palette.fgMute} />
        ) : isImage ? (
          <SymbolView
            accessibilityElementsHidden
            name={preview ? "chevron.up" : "eye"}
            size={15}
            tintColor={palette.fgMute}
          />
        ) : null}
      </Pressable>
      {failed ? <Text style={styles.rowMeta}>Couldn’t load preview.</Text> : null}
      {preview ? (
        <Image
          source={{ uri: preview }}
          style={styles.imagePreview}
          contentFit="contain"
          cachePolicy="memory"
          accessibilityLabel={name}
        />
      ) : null}
    </View>
  );
}

export function AttachmentsSection({ issueId, attachments }: { issueId: string; attachments: IssueAttachment[] }) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  if (attachments.length === 0) return null;
  return (
    <View style={styles.section}>
      <Text style={styles.overline}>
        ATTACHMENTS
        <Text style={styles.sectionCount}> · {attachments.length}</Text>
      </Text>
      {attachments.map((attachment) => (
        <AttachmentRow key={attachment.id} issueId={issueId} attachment={attachment} />
      ))}
    </View>
  );
}

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    section: { gap: spacing.sm },
    overline: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.9,
      color: p.fgMute,
    },
    sectionCount: { color: p.fgMute },

    card: {
      backgroundColor: p.surface,
      borderRadius: radii.md,
      borderWidth: 1,
      borderColor: p.edge,
      padding: spacing.md,
      gap: spacing.sm,
    },
    rowHead: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
    rowIcon: { marginTop: 1 },
    rowText: { flex: 1, gap: 2 },
    rowTitle: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
    },
    rowMeta: {
      fontFamily: fonts.sans,
      fontSize: 11,
      lineHeight: 15,
      color: p.fgMute,
    },
    linkAction: {
      fontFamily: fonts.sansMedium,
      fontSize: 12,
      lineHeight: 16,
      color: p.link,
    },

    reviewRow: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.sm,
      flexWrap: "wrap",
    },
    reviewLabel: {
      flex: 1,
      fontFamily: fonts.sansMedium,
      fontSize: 12,
      lineHeight: 16,
      color: p.warn,
    },
    pressed: { opacity: 0.6 },
    approveBtn: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.xs,
      paddingHorizontal: spacing.md,
      paddingVertical: 6,
      borderRadius: radii.sm,
      backgroundColor: p.fg,
    },
    approveText: {
      fontFamily: fonts.sansMedium,
      fontSize: 12,
      lineHeight: 16,
      color: p.onFg,
    },
    changesBtn: {
      paddingHorizontal: spacing.md,
      paddingVertical: 6,
      borderRadius: radii.sm,
      borderWidth: 1,
      borderColor: p.edge,
      backgroundColor: p.surface,
    },
    changesText: {
      fontFamily: fonts.sansMedium,
      fontSize: 12,
      lineHeight: 16,
      color: p.fgMute,
    },

    imagePreview: {
      width: "100%",
      height: 220,
      borderRadius: radii.sm,
      backgroundColor: p.raised,
    },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };
