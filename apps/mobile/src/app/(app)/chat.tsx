import { useAuth } from "@clerk/clerk-expo";
import { useCallback, useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { ApiError, fetchJsonWithStatus } from "@isol8/shared";

import { API_URL } from "../../config/env";
import { makeAdapter } from "../../lib/adapter";

/**
 * Phase 1 proof-of-plumbing: the same GET /container/status poll the web
 * onboarding flow makes (200 = container info, 202 = workspace pending),
 * through the shared client with the mobile adapter. Replaced by the real
 * agent list + chat timeline in Phase 2.
 */
export default function Chat() {
  const { getToken } = useAuth();
  const [result, setResult] = useState<string>("Checking container…");

  const check = useCallback(async () => {
    setResult("Checking container…");
    try {
      const { status, body } = await fetchJsonWithStatus(
        makeAdapter(getToken),
        "/container/status",
      );
      const summary =
        body && typeof body === "object" && "status" in body
          ? String((body as { status: unknown }).status)
          : "ok";
      setResult(`HTTP ${status} — container: ${summary}`);
    } catch (e) {
      if (e instanceof ApiError) {
        setResult(`HTTP ${e.status} — ${e.message}`);
      } else {
        setResult(e instanceof Error ? e.message : "Request failed");
      }
    }
  }, [getToken]);

  useEffect(() => {
    check();
  }, [check]);

  return (
    <View style={styles.container}>
      <Text style={styles.heading}>Chat</Text>
      <Text style={styles.status}>{result}</Text>
      <Pressable style={styles.button} onPress={check}>
        <Text style={styles.buttonText}>Re-check</Text>
      </Pressable>
      <Text style={styles.meta}>{API_URL}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: "center", justifyContent: "center", gap: 10, padding: 24 },
  heading: { fontSize: 24, fontWeight: "600" },
  status: { fontSize: 15, textAlign: "center" },
  button: {
    backgroundColor: "#1a1a1a",
    borderRadius: 8,
    paddingHorizontal: 18,
    paddingVertical: 10,
    marginTop: 6,
  },
  buttonText: { color: "#fff", fontSize: 14, fontWeight: "600" },
  meta: { fontSize: 11, opacity: 0.4, marginTop: 16 },
});
