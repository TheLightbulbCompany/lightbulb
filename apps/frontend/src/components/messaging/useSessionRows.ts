"use client";

import { useMemo } from "react";
import { mergeSessionRows, type LocalSession, type SessionRow } from "@lightbulb/shared";
import { useEngineDirectory } from "./engineDirectory";

/** The sessions the sidebar lists: the backend's, joined to this desktop's engine directory when it has one (spec 2026-10-30 §10.4). */
export function useSessionRows(
  sessions: readonly LocalSession[],
  userId: string | null | undefined,
  nameOf: (userId: string) => string,
): SessionRow[] {
  const { records } = useEngineDirectory();
  return useMemo(() => mergeSessionRows(sessions, records, { userId, nameOf }), [sessions, records, userId, nameOf]);
}
