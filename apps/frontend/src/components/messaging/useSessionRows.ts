"use client";

import { useMemo } from "react";
import { mergeSessionRows, type LocalSession, type SessionRow } from "@lightbulb/shared";

const NO_ENGINE_ROWS: never[] = [];

/** The sessions the sidebar lists. Backend rows only until the desktop reads its engine (spec 2026-10-30 §10.4). */
export function useSessionRows(
  sessions: readonly LocalSession[],
  userId: string | null | undefined,
  nameOf: (userId: string) => string,
): SessionRow[] {
  return useMemo(() => mergeSessionRows(sessions, NO_ENGINE_ROWS, { userId, nameOf }), [sessions, userId, nameOf]);
}
