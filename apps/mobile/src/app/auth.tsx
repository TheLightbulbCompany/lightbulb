import { useSignIn } from "@clerk/clerk-expo";
import { type Href, useLocalSearchParams, useRouter } from "expo-router";
import { useEffect, useRef, useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

import { colors, radii, spacing, type } from "../config/theme";

function safeReturnPath(value: string | string[] | undefined): Href {
  if (
    typeof value === "string" &&
    value.startsWith("/") &&
    !value.startsWith("//") &&
    !value.includes("\\")
  ) {
    return value as Href;
  }
  return "/";
}

export default function AuthHandoff() {
  const { ticket, returnTo } = useLocalSearchParams<{
    ticket?: string | string[];
    returnTo?: string | string[];
  }>();
  const { isLoaded, signIn, setActive } = useSignIn();
  const router = useRouter();
  const started = useRef(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!isLoaded || started.current) return;
    started.current = true;

    if (typeof ticket !== "string" || !ticket) {
      setFailed(true);
      return;
    }

    void signIn
      .create({ strategy: "ticket", ticket })
      .then(async (attempt) => {
        if (attempt.status !== "complete" || !attempt.createdSessionId) {
          throw new Error("Incomplete ticket sign-in");
        }
        await setActive({ session: attempt.createdSessionId });
        router.replace(safeReturnPath(returnTo));
      })
      .catch(() => setFailed(true));
  }, [isLoaded, returnTo, router, setActive, signIn, ticket]);

  if (!failed) {
    return (
      <View style={styles.container}>
        <ActivityIndicator color={colors.forest} />
        <Text style={styles.body}>Finishing sign in…</Text>
      </View>
    );
  }

  return (
    <View style={styles.container}>
      <Text style={styles.title}>We couldn't finish signing you in.</Text>
      <Pressable style={styles.button} onPress={() => router.replace("/(auth)/sign-in")}>
        <Text style={styles.buttonText}>Sign in in the app</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.md,
    padding: spacing.xl,
    backgroundColor: colors.cream,
  },
  title: { ...type.heading, textAlign: "center" },
  body: { ...type.body, textAlign: "center" },
  button: {
    backgroundColor: colors.forest,
    borderRadius: radii.lg,
    paddingHorizontal: spacing.xl,
    paddingVertical: spacing.md,
  },
  buttonText: { ...type.body, color: colors.white },
});
