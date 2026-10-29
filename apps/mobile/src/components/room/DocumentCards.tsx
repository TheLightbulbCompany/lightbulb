import { useAuth } from "@clerk/expo";
import {
  createApiClient,
  documentPreviewText,
  extractDocumentLinks,
  useDocumentPreview,
  type ApiMethods,
  type DocumentLink,
} from "@lightbulb/shared";
import { SymbolView } from "../ui/SymbolView";
import { useMemo, useRef, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { makeAdapter } from "../../lib/adapter";
import { relativeTime } from "../../lib/relativeTime";
import { MessageMarkdown } from "../chat/MessageMarkdown";
import { BottomSheet } from "../ui/BottomSheet";

/**
 * A card for each Paperclip document a message links (`/<PFX>/issues/<ID>#document-<key>`),
 * the phone twin of web's RoomDocumentCards: title, task, a few lines, last updated. Tap
 * opens the document in a sheet over the conversation. Read through the company-fenced
 * Teams BFF; anything that fails renders nothing.
 */
export function DocumentCards({ text }: { text: string }) {
  const links = useMemo(() => extractDocumentLinks(text), [text]);
  return links.length ? <DocumentCardList links={links} /> : null;
}

function DocumentCardList({ links }: { links: DocumentLink[] }) {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);
  return (
    <View style={styles.list}>
      {links.map((link) => (
        <DocumentCard key={`${link.issueId}#${link.key}`} api={api} link={link} />
      ))}
    </View>
  );
}

function DocumentCard({ api, link }: { api: ApiMethods; link: DocumentLink }) {
  const preview = useDocumentPreview(api, link);
  const [open, setOpen] = useState(false);
  if (!preview) return null;
  const { issue, document } = preview;
  const title = document.title?.trim() || document.key;
  const excerpt = documentPreviewText(document.body ?? "", title);
  const updatedAt = relativeTime(Date.parse(document.updatedAt ?? "") || undefined);
  const updated = updatedAt ? `Updated ${updatedAt}` : "";
  const task = `${issue.identifier} · ${issue.title}`;
  return (
    <>
      <Pressable
        testID="document-card"
        onPress={() => setOpen(true)}
        accessibilityRole="button"
        accessibilityLabel={`Open ${title}, ${issue.identifier}`}
        style={({ pressed }) => [styles.card, pressed && styles.cardPressed]}
      >
        <View style={styles.head}>
          <SymbolView accessibilityElementsHidden name="doc.text" size={15} tintColor={colors.mute} />
          <Text style={styles.title} numberOfLines={2}>
            {title}
          </Text>
        </View>
        <Text style={styles.meta} numberOfLines={1}>
          {task}
        </Text>
        {excerpt ? (
          <Text style={styles.excerpt} numberOfLines={3}>
            {excerpt}
          </Text>
        ) : null}
        <View style={styles.foot}>
          <Text style={styles.meta} numberOfLines={1}>
            {updated}
          </Text>
          <Text style={styles.open}>Open</Text>
        </View>
      </Pressable>
      <BottomSheet visible={open} onClose={() => setOpen(false)} large>
        <View style={styles.sheetHead}>
          <Text style={styles.sheetTitle}>{title}</Text>
          <Text style={styles.meta}>{task}</Text>
        </View>
        <ScrollView contentContainerStyle={styles.sheetBody}>
          {document.body?.trim() ? (
            <MessageMarkdown text={document.body} />
          ) : (
            <Text style={styles.meta}>Empty document.</Text>
          )}
        </ScrollView>
      </BottomSheet>
    </>
  );
}

const styles = StyleSheet.create({
  list: { gap: spacing.sm, marginTop: spacing.xs },
  card: {
    maxWidth: 420,
    gap: 2,
    paddingHorizontal: spacing.md,
    paddingTop: spacing.sm,
    paddingBottom: spacing.sm + 2,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.lg,
    backgroundColor: colors.cream,
  },
  cardPressed: { backgroundColor: colors.rowPressed },
  head: { flexDirection: "row", alignItems: "center", gap: 6 },
  title: { flexShrink: 1, fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 19, color: colors.ink },
  excerpt: { marginTop: 2, fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.mute },
  foot: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: spacing.sm, marginTop: 4 },
  meta: { flexShrink: 1, fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute },
  open: { fontFamily: fonts.sansSemiBold, fontSize: 12, lineHeight: 16, color: colors.forest },
  sheetHead: { gap: 2, paddingHorizontal: spacing.lg, paddingBottom: spacing.sm },
  sheetTitle: { fontFamily: fonts.sansSemiBold, fontSize: 17, lineHeight: 22, color: colors.ink },
  sheetBody: { paddingHorizontal: spacing.lg, paddingBottom: spacing.xl },
});
