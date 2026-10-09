import { useAuth } from "@clerk/expo";
import { createApiClient, extractPreviewUrls, linkDomain, useLinkPreview, type ApiMethods, type LinkPreview } from "@lightbulb/shared";
import { Image } from "expo-image";
import { useMemo, useRef, useState } from "react";
import { Linking, Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { makeAdapter } from "../../lib/adapter";

/**
 * iMessage-style cards for the first URLs in a message, from the backend's
 * server-side fetch (the phone never fetches the page). Anything that fails,
 * including an older backend without the endpoint, renders nothing.
 */
export function LinkPreviews({ text }: { text: string }) {
  const urls = useMemo(() => extractPreviewUrls(text), [text]);
  return urls.length ? <PreviewList urls={urls} /> : null;
}

function PreviewList({ urls }: { urls: string[] }) {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);
  return (
    <View style={styles.list}>
      {urls.map((url) => (
        <Preview key={url} api={api} url={url} />
      ))}
    </View>
  );
}

function Preview({ api, url }: { api: ApiMethods; url: string }) {
  const preview = useLinkPreview(api, url);
  return preview ? <LinkCard preview={preview} /> : null;
}

function formatDate(value: string | null): string | null {
  if (!value || Number.isNaN(Date.parse(value))) return null;
  return new Date(value).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function LinkCard({ preview }: { preview: LinkPreview }) {
  const [imageFailed, setImageFailed] = useState(false);
  const tweet = preview.kind === "tweet";
  const meta = [preview.siteName || linkDomain(preview.url), formatDate(preview.publishedAt)].filter(Boolean).join(" · ");
  return (
    <Pressable
      testID="link-preview"
      onPress={() => void Linking.openURL(preview.url).catch(() => {})}
      accessibilityRole="link"
      accessibilityLabel={`${tweet ? `${preview.author ?? preview.title} on X` : preview.title}, ${linkDomain(preview.url)}`}
      style={({ pressed }) => [styles.card, pressed && styles.cardPressed]}
    >
      {preview.imageUrl && !imageFailed ? (
        <Image
          source={{ uri: preview.imageUrl }}
          style={styles.image}
          contentFit="cover"
          cachePolicy="memory-disk"
          onError={() => setImageFailed(true)}
          accessibilityIgnoresInvertColors
        />
      ) : null}
      <View style={styles.body}>
        {tweet ? (
          <View style={styles.author}>
            {preview.avatarUrl ? (
              <Image source={{ uri: preview.avatarUrl }} style={styles.avatar} cachePolicy="memory-disk" />
            ) : null}
            <Text style={styles.title} numberOfLines={1}>
              {preview.author ?? preview.title}
            </Text>
            {preview.handle ? (
              <Text style={styles.meta} numberOfLines={1}>
                @{preview.handle}
              </Text>
            ) : null}
          </View>
        ) : (
          <Text style={styles.title} numberOfLines={2}>
            {preview.title}
          </Text>
        )}
        {preview.description ? (
          <Text style={tweet ? styles.tweetText : styles.description} numberOfLines={tweet ? 6 : 2}>
            {preview.description}
          </Text>
        ) : null}
        <Text style={styles.meta} numberOfLines={1}>
          {meta}
        </Text>
      </View>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  list: { gap: spacing.sm, marginTop: spacing.xs },
  card: {
    maxWidth: 420,
    overflow: "hidden",
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.lg,
    backgroundColor: colors.cream,
  },
  cardPressed: { backgroundColor: colors.rowPressed },
  image: { width: "100%", aspectRatio: 1.91, backgroundColor: colors.skeleton },
  body: { gap: 2, paddingHorizontal: spacing.md, paddingTop: spacing.sm, paddingBottom: spacing.sm + 2 },
  author: { flexDirection: "row", alignItems: "center", gap: 6 },
  avatar: { width: 20, height: 20, borderRadius: 10, backgroundColor: colors.skeleton },
  title: { flexShrink: 1, fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 19, color: colors.ink },
  description: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.mute },
  tweetText: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, color: colors.ink },
  meta: { flexShrink: 1, fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute },
});
