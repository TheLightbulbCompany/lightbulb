/**
 * Blocking AI data-sharing disclosure — App Store guidelines 5.1.1(i) and
 * 5.1.2(i), the third rejection reason on 0.1.0 (17).
 *
 * Apple requires all four of: say what data is sent, say who receives it, get
 * permission BEFORE sending, and carry the same detail in the privacy policy.
 * They explicitly ruled that the Terms/Privacy Policy alone do not satisfy the
 * first three — hence a gate the user has to answer rather than a link.
 *
 * Mounted ABOVE ProvisionGate in (app)/_layout.tsx, i.e. the first thing after
 * the auth check, because every surface below it eventually reaches an agent:
 * chat obviously, but also the first-run brief (written to BRIEF.md and read by
 * Friday) and a Teams comment (which can start a run). Gating chat alone would
 * leave those two paths sending content on no consent at all. One gate at the
 * top is both the smaller diff and the only placement without a hole.
 *
 * Declining signs the user out rather than dropping them into a crippled shell:
 * consent is a precondition for the product doing anything, so "no" has to be a
 * real exit, and the account (and its web access) is untouched by it.
 *
 * The recipient list is deliberately the FULL set an owner can be switched to
 * — OpenRouter and the model hosts it routes to, AWS (hosting, plus workspace
 * memory processed by AWS-managed models), Anthropic or OpenAI once the owner
 * connects their own key or ChatGPT sign-in, and Vercel/TypeSafe for Listening
 * routing — not just the provider active now.
 * A provider swap made later on the web must not silently widen an agreement
 * that named fewer companies (settings.tsx `providerLabel` is where the live
 * choice is surfaced).
 */
import { useAuth, useUser } from "@clerk/expo";
import { useEffect, useState, type ReactNode } from "react";
import {
  ActivityIndicator,
  Linking,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { WEB_ORIGIN } from "../../config/domain";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { colors, fonts, radii, spacing, type } from "../../config/theme";
import { readAiConsent, writeAiConsent } from "../../lib/aiConsent";

const PRIVACY_URL = `${WEB_ORIGIN}/privacy`;

/** What leaves the device, in the user's terms — not a data-category taxonomy. */
const DATA_SENT = [
  "Messages you send to your agents, and their replies",
  "Files and images you attach to a message",
  "Content your agents read or produce while working on your tasks",
  "When Listening is on in a channel: its recent messages (with sender names) and short descriptions of your agents, so the right agent picks up the conversation. Attachments, email addresses and account details are never sent for this.",
];

const RECIPIENTS = [
  "OpenRouter — routes your requests to the AI model that runs your agents",
  "The AI model providers OpenRouter routes to — companies such as DeepInfra that run open models like DeepSeek",
  "Amazon Web Services — hosts our infrastructure and stores and processes workspace memory using AWS-managed AI models",
  "Anthropic — only if you connect your own Anthropic key",
  "OpenAI — only if you connect a ChatGPT account or your own OpenAI key",
  "Vercel and TypeSafe AI — decide which agent answers in channels with Listening on; zero data retention on every request",
];

export function AiConsentGate({ children }: { children: ReactNode }) {
  const { user } = useUser();
  const { signOut } = useAuth();
  const userId = user?.id;

  // undefined = keychain not read yet. Distinct from `false` (asked, declined
  // or never answered): only `true` may render children.
  // Hoisted above every early return: hooks must run in the same order each render.
  const insets = useSafeAreaInsets();
  const [granted, setGranted] = useState<boolean | undefined>(undefined);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!userId) return;
    let cancelled = false;
    void readAiConsent(userId).then((value) => {
      if (!cancelled) setGranted(value);
    });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  async function onAgree() {
    if (!userId || saving) return;
    setSaving(true);
    try {
      await writeAiConsent(userId);
      setGranted(true);
    } catch {
      // A keychain write failure must not wave the user through: stay on the
      // gate (readAiConsent would fail closed next launch anyway) and let them
      // tap again.
      setSaving(false);
    }
  }

  // No user yet, or the latch is still being read — render nothing rather than
  // flashing the disclosure at someone who already agreed. Matches the
  // `if (!isLoaded) return null` frame the layout above already uses.
  if (!userId || granted === undefined) return null;
  if (granted) return <>{children}</>;

  return (
    // This screen mounts above the navigator, so no header pushes it below the
    // status bar / Dynamic Island — it has to honour the inset itself. The
    // inset goes on a wrapper, not the scroll content: content padding scrolls
    // away with the text, which then slides under the clock and battery.
    <View style={[styles.page, { paddingTop: insets.top }]}>
      <ScrollView
        style={styles.page}
        contentContainerStyle={styles.content}
        testID="ai-consent-gate"
      >
        <Text style={styles.title}>Before you start</Text>
        <Text style={styles.lede}>
          Lightbulb runs your agents on AI models operated by other companies. To do
          that, it has to send them what you and your agents are working on.
        </Text>

        <View style={styles.card}>
          <Text style={styles.overline}>WHAT IS SENT</Text>
          {DATA_SENT.map((item) => (
            <Text key={item} style={styles.item}>
              •  {item}
            </Text>
          ))}
        </View>

        <View style={styles.card}>
          <Text style={styles.overline}>WHO IT IS SENT TO</Text>
          {RECIPIENTS.map((item) => (
            <Text key={item} style={styles.item}>
              •  {item}
            </Text>
          ))}
          <Text style={styles.caption}>
            {"These providers process your content only to generate a response. OpenRouter routes your requests only to model providers that don't store your content or use it for training, and Lightbulb never sells your data or trains on it."}
          </Text>
        </View>

        <Pressable
          onPress={() => void Linking.openURL(PRIVACY_URL)}
          accessibilityRole="link"
          style={({ pressed }) => [styles.linkRow, pressed && styles.pressed]}
        >
          <Text style={styles.linkText}>Read the full Privacy Policy</Text>
        </Pressable>

        <Pressable
          onPress={() => void onAgree()}
          disabled={saving}
          accessibilityRole="button"
          style={({ pressed }) => [
            styles.primary,
            (pressed || saving) && styles.pressed,
          ]}
        >
          {saving ? (
            <ActivityIndicator color={colors.white} />
          ) : (
            <Text style={styles.primaryText}>Agree and continue</Text>
          )}
        </Pressable>

        <Pressable
          onPress={() => void signOut()}
          disabled={saving}
          accessibilityRole="button"
          style={({ pressed }) => [styles.secondary, pressed && styles.pressed]}
        >
          <Text style={styles.secondaryText}>Don’t agree — sign out</Text>
        </Pressable>
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  content: { padding: spacing.xl, gap: spacing.lg },
  title: { ...type.display },
  lede: { ...type.body, color: colors.inkSoft },
  card: {
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.lg,
    gap: spacing.sm,
  },
  overline: { ...type.overline },
  item: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.ink,
  },
  caption: { ...type.caption, marginTop: spacing.xs },
  linkRow: { alignSelf: "flex-start", borderRadius: radii.xs },
  linkText: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.forest,
  },
  primary: {
    backgroundColor: colors.forest,
    borderRadius: radii.lg,
    paddingVertical: spacing.md,
    alignItems: "center",
  },
  primaryText: {
    fontFamily: fonts.sansMedium,
    fontSize: 15,
    lineHeight: 22,
    color: colors.white,
  },
  secondary: {
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    paddingVertical: spacing.md,
    alignItems: "center",
  },
  secondaryText: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.inkSoft,
  },
  pressed: { opacity: 0.6 },
});
