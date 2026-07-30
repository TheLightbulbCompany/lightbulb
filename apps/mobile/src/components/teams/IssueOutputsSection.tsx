import Feather from "@expo/vector-icons/Feather";
import * as WebBrowser from "expo-web-browser";
import { useState } from "react";
import {
  ActivityIndicator,
  Image,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { colors, fonts, radii, spacing, type } from "../../config/theme";
import { MessageMarkdown } from "../chat/MessageMarkdown";
import {
  fetchAttachmentDataUri,
  timeAgo,
  useTeamsConfig,
  type IssueAttachment,
  type IssueDocument,
  type IssueWorkProduct,
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

const WORK_PRODUCT_ICON: Record<string, keyof typeof Feather.glyphMap> = {
  preview_url: "globe",
  runtime_service: "server",
  pull_request: "git-pull-request",
  branch: "git-branch",
  commit: "git-commit",
  artifact: "package",
  document: "file-text",
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
              <Feather
                name={WORK_PRODUCT_ICON[wp.type] ?? "box"}
                size={15}
                color={colors.forest}
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
                  onPress={() => void WebBrowser.openBrowserAsync(href)}
                  hitSlop={6}
                  accessibilityRole="button"
                  accessibilityLabel={`Open ${wp.title}`}
                >
                  <Text style={styles.linkAction}>Open</Text>
                </Pressable>
              ) : null}
            </View>
            {wp.summary ? <MessageMarkdown text={wp.summary} /> : null}
            {needsReview ? (
              <View style={styles.reviewRow}>
                <Text style={styles.reviewLabel}>Needs your review</Text>
                <Pressable
                  style={({ pressed }) => [styles.approveBtn, pressed && styles.pressed]}
                  onPress={() => onReview(wp.id, "approved")}
                  accessibilityRole="button"
                  accessibilityLabel={`Approve ${wp.title}`}
                >
                  <Feather name="check" size={13} color={colors.white} />
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

export function DocumentsSection({ documents }: { documents: IssueDocument[] }) {
  const [openKeys, setOpenKeys] = useState<Set<string>>(new Set());
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
              <Feather name="file-text" size={15} color={colors.forest} style={styles.rowIcon} />
              <View style={styles.rowText}>
                <Text style={styles.rowTitle} numberOfLines={1}>
                  {doc.title ?? doc.key}
                </Text>
                {doc.updatedAt ? (
                  <Text style={styles.rowMeta}>updated {timeAgo(doc.updatedAt)}</Text>
                ) : null}
              </View>
              <Feather
                name={open ? "chevron-up" : "chevron-down"}
                size={15}
                color={colors.muteSoft}
              />
            </Pressable>
            {open ? (
              doc.body ? (
                <MessageMarkdown text={doc.body} />
              ) : (
                <Text style={styles.rowMeta}>Empty document.</Text>
              )
            ) : null}
          </View>
        );
      })}
    </View>
  );
}

/** Inline preview cap: only fetch data URIs for images up to 4 MB — a base64
 * data URI triples memory, and anything bigger belongs on web. */
const IMAGE_PREVIEW_MAX_BYTES = 4 * 1024 * 1024;

function AttachmentRow({ attachment }: { attachment: IssueAttachment }) {
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
          void fetchAttachmentDataUri(config, attachment.id).then((uri) => {
            setLoading(false);
            if (uri) setPreview(uri);
            else setFailed(true);
          });
        }}
        accessibilityRole={isImage ? "button" : "none"}
        accessibilityLabel={isImage ? `Preview ${name}` : name}
      >
        <Feather
          name={isImage ? "image" : "paperclip"}
          size={15}
          color={colors.forest}
          style={styles.rowIcon}
        />
        <View style={styles.rowText}>
          <Text style={styles.rowTitle} numberOfLines={1}>
            {name}
          </Text>
          {meta !== "" && <Text style={styles.rowMeta}>{meta}</Text>}
        </View>
        {loading ? (
          <ActivityIndicator size="small" color={colors.mute} />
        ) : isImage ? (
          <Feather
            name={preview ? "chevron-up" : "eye"}
            size={15}
            color={colors.muteSoft}
          />
        ) : null}
      </Pressable>
      {failed ? <Text style={styles.rowMeta}>Couldn’t load preview.</Text> : null}
      {preview ? (
        <Image
          source={{ uri: preview }}
          style={styles.imagePreview}
          resizeMode="contain"
          accessibilityLabel={name}
        />
      ) : null}
    </View>
  );
}

export function AttachmentsSection({ attachments }: { attachments: IssueAttachment[] }) {
  if (attachments.length === 0) return null;
  return (
    <View style={styles.section}>
      <Text style={styles.overline}>
        ATTACHMENTS
        <Text style={styles.sectionCount}> · {attachments.length}</Text>
      </Text>
      {attachments.map((attachment) => (
        <AttachmentRow key={attachment.id} attachment={attachment} />
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  section: { gap: spacing.sm },
  overline: { ...type.overline },
  sectionCount: { color: colors.sectionCount },

  card: {
    backgroundColor: colors.white,
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.md,
    gap: spacing.sm,
  },
  rowHead: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  rowIcon: { marginTop: 1 },
  rowText: { flex: 1, gap: 2 },
  rowTitle: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },
  rowMeta: {
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
  linkAction: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.forest,
  },

  reviewRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    flexWrap: "wrap",
  },
  reviewLabel: {
    flex: 1,
    fontFamily: fonts.dmSansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.warn,
  },
  pressed: { opacity: 0.6 },
  approveBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    paddingHorizontal: spacing.md,
    paddingVertical: 6,
    borderRadius: radii.sm,
    backgroundColor: colors.forest,
  },
  approveText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.white,
  },
  changesBtn: {
    paddingHorizontal: spacing.md,
    paddingVertical: 6,
    borderRadius: radii.sm,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.white,
  },
  changesText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },

  imagePreview: {
    width: "100%",
    height: 220,
    borderRadius: radii.sm,
    backgroundColor: colors.creamDeeper,
  },
});
