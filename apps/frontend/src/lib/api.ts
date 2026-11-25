import { useMemo } from "react";
import { useAuth } from "@clerk/nextjs";
import { recordRequestId } from "./requestIdLog";

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

/** Authenticated JSON POST to the backend, with the Clerk bearer token. Every response's request id feeds the support ring buffer. */
export function useApi(): { post: (endpoint: string, body: unknown) => Promise<unknown> } {
  const { getToken } = useAuth();

  return useMemo(
    () => ({
      async post(endpoint: string, body: unknown): Promise<unknown> {
        const token = await getToken();
        if (!token) throw new Error("No authentication token available");
        const response = await fetch(`${BACKEND_URL}${endpoint}`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        recordRequestId(response.headers.get("X-Request-ID"));
        if (!response.ok) throw new Error(`API ${response.status}`);
        return response.status === 204 ? null : response.json();
      },
    }),
    [getToken],
  );
}
