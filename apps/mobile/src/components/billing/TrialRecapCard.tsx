/**
 * Day-28 recap on the phone's home list (T19, docs/marketing/15 §B): the team's
 * own numbers before the ask. Admins open Plans; members are pointed at an
 * admin. "Not now" hides it for three days on this device.
 *
 * While the server sells no plan in the app (IAP off), the card shows no price
 * and no plan button: a price or purchase prompt in the iPhone app without
 * In-App Purchase is an App Store 3.1.1 rejection.
 */
import { useAuth, useOrganization } from "@clerk/expo";
import { router } from "expo-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { createApiClient } from "@lightbulb/shared";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";
import { makeAdapter } from "../../lib/adapter";
import { paywallProductIds } from "../../lib/appleIap";
import { LocalStore } from "../../lib/localStore";

type Recap = {
  show: boolean;
  trial_end?: number;
  handoffs?: number;
  approvals?: number;
  agents?: number;
  people?: number;
  top_item?: string | null;
};
type Prices = {
  workspace_price_usd?: number;
  included_members?: number;
  apple_iap_product_ids?: string[] | null;
};

const store = new LocalStore<{ dismissedUntil?: number }>("lightbulb.trialRecap");
const DISMISS_MS = 3 * 86_400_000;

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** Same rule as the web card: a part shows only when its count is above zero. */
export function recapLine(r: Recap): string | null {
  const parts = [
    (r.handoffs ?? 0) > 0 && plural(r.handoffs!, "handoff", "handoffs"),
    (r.approvals ?? 0) > 0 && plural(r.approvals!, "approval", "approvals"),
    (r.agents ?? 0) > 0 &&
      `${plural(r.agents!, "agent", "agents")} did work${(r.people ?? 0) > 0 ? ` for ${plural(r.people!, "person", "people")}` : ""}`,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

export function TrialRecapCard() {
  const palette = useChatPalette();
  const { getToken } = useAuth();
  const { membership } = useOrganization();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);
  const [recap, setRecap] = useState<Recap | null>(null);
  const [prices, setPrices] = useState<Prices | null>(null);
  const [hidden, setHidden] = useState(() => (store.read().dismissedUntil ?? 0) > Date.now());

  useEffect(() => {
    let live = true;
    void api
      .get("/billing/trial-recap")
      .then((body) => live && setRecap(body as Recap))
      .catch(() => {});
    void api
      .get("/billing/account")
      .then((body) => live && setPrices(body as Prices))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [api]);

  if (!recap?.show || hidden || recap.trial_end == null) return null;
  const line = recapLine(recap);
  const sellsInApp = paywallProductIds(prices).length > 0;
  const nothingFinished = !recap.handoffs && !recap.approvals;
  const ends = new Date(recap.trial_end * 1000).toLocaleDateString(undefined, {
    timeZone: "UTC",
    month: "long",
    day: "numeric",
  });
  const text = { color: palette.fgSoft, fontFamily: fonts.sans, fontSize: 14, lineHeight: 20 };

  return (
    <View
      testID="trial-recap"
      style={[styles.card, { backgroundColor: palette.surface, borderColor: palette.edge }]}
    >
      <Text style={{ color: palette.fg, fontFamily: fonts.sansSemiBold, fontSize: 15 }}>
        Here&apos;s what your team did in 30 days
      </Text>
      {nothingFinished ? (
        <Text style={text}>Your agents haven&apos;t finished anything for the team yet.</Text>
      ) : (
        <>
          {line != null && <Text style={text}>{line}</Text>}
          {recap.top_item ? <Text style={text}>{recap.top_item}</Text> : null}
        </>
      )}
      {sellsInApp && prices?.workspace_price_usd != null && (
        <Text style={[text, { color: palette.fg }]}>
          {`Keep going for $${prices.workspace_price_usd} a month. People are free up to ${prices.included_members ?? 5}.`}
        </Text>
      )}
      <View style={styles.row}>
        {!sellsInApp ? null : membership?.role === "org:admin" ? (
          <Pressable accessibilityRole="button" onPress={() => router.push("/plans")}>
            <Text style={{ color: palette.fg, fontFamily: fonts.sansMedium, fontSize: 15 }}>Keep the team</Text>
          </Pressable>
        ) : (
          <Text style={text}>Ask an admin to keep the team.</Text>
        )}
        <Pressable
          accessibilityRole="button"
          onPress={() => {
            store.write({ dismissedUntil: Date.now() + DISMISS_MS });
            setHidden(true);
          }}
        >
          <Text style={text}>Not now</Text>
        </Pressable>
      </View>
      <Text style={[text, { fontSize: 12 }]}>
        {sellsInApp
          ? `Your trial ends ${ends}. After that everything stays readable; agents stop until someone picks the plan.`
          : `Your trial ends ${ends}. After that everything stays readable.`}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: { marginHorizontal: spacing.md, marginBottom: spacing.sm, padding: spacing.md, gap: spacing.xs, borderRadius: radii.lg, borderWidth: 1 },
  row: { flexDirection: "row", alignItems: "center", gap: spacing.lg, marginTop: spacing.xs },
});
