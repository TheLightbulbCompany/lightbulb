"use client";

// Lane A's automation routes (routers/automations.py): actions and the viewer's two predicates. Reads go
// through the shared hooks (`useAutomations(useAutomationSource())`).

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { useAuth, useOrganization } from "@clerk/nextjs";
import {
  automationKey, automationRunsKey, automationsKey, canDecide, canEdit, deliveryRoomId, deliveryWords, roomDisplayName, specialistLabel, runNow as runNowShared,
  type AutomationSource, type AutomationView, type RunNowOutcome,
} from "@lightbulb/shared";
import { useMessagingWorkspaceOptional } from "@/components/messaging/MessagingWorkspaceContext";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { useApi } from "@/lib/api";
import { errorCopy } from "./useTaskActions";

export type AutomationVerb = "activate" | "pause" | "resume" | "run";

/** What the shared automation hooks read through: the signed-in API client and the workspace scope. */
export function useAutomationSource(): AutomationSource {
  return { api: useApi(), cacheScope: useLiveEvents().tasks.cacheScope };
}

/** One POST per press; the list, the row and its runs re-read afterwards. */
export function useAutomationActions() {
  const api = useApi();
  const { tasks } = useLiveEvents();
  const { mutate } = useSWRConfig();
  const refresh = useCallback((id: string) => Promise.all([
    mutate(automationsKey(tasks.cacheScope)), mutate(automationKey(tasks.cacheScope, id)), mutate(automationRunsKey(tasks.cacheScope, id)),
  ]), [mutate, tasks.cacheScope]);
  const act = useCallback(async (id: string, verb: AutomationVerb): Promise<AutomationView | { triggerId: string }> => {
    const res = await api.post(`/automations/${encodeURIComponent(id)}/${verb}`, {}) as AutomationView | { triggerId: string };
    await refresh(id);
    return res;
  }, [api, refresh]);
  const remove = useCallback(async (id: string) => {
    await api.del(`/automations/${encodeURIComponent(id)}`);
    await refresh(id);
  }, [api, refresh]);
  /** PATCH /automations/{id} with the row's version (a stale one is a 409 the caller shows). */
  const edit = useCallback(async (id: string, body: { instructions?: string; schedule?: { cron: string[]; tz: string; remainingRuns?: number | null } }, version: number) => {
    await api.patch(`/automations/${encodeURIComponent(id)}`, { ...body, version });
    await refresh(id);
  }, [api, refresh]);
  return { act, remove, edit, refresh };
}

/** Who may turn a proposal on or resume it (shared `canDecide`, the server's two predicates). */
export function useCanDecide(): (a: Pick<AutomationView, "deliverTo" | "ownerUserId" | "match">) => boolean {
  const { userId } = useAuth();
  const { membership } = useOrganization();
  const admin = membership?.role === "org:admin";
  return useCallback((a) => canDecide(a, { userId: userId ?? null, admin }), [admin, userId]);
}

/** Who may pause it, run it now or drop a proposal (the server's `_may_edit`): an org admin or its owner. */
export function useCanEdit(): (a: Pick<AutomationView, "ownerUserId">) => boolean {
  const { userId } = useAuth();
  const { membership } = useOrganization();
  const admin = membership?.role === "org:admin";
  return useCallback((a) => canEdit(a, { userId: userId ?? null, admin }), [admin, userId]);
}

export type { RunNowOutcome };
export const runNow = (act: (id: string, verb: AutomationVerb) => Promise<unknown>, a: Pick<AutomationView, "id" | "activeRunTaskId" | "status">): Promise<RunNowOutcome> =>
  runNowShared((id) => act(id, "run"), a, errorCopy);

/** "HH:MM" of a cron list's first entry (wall clock in the row's zone), or null when it isn't a plain time. */
export function cronTime(cron: readonly string[]): string | null {
  const [min, hour] = (cron[0] ?? "").trim().split(/\s+/);
  return /^\d+$/.test(min ?? "") && /^\d+$/.test(hour ?? "") ? `${hour.padStart(2, "0")}:${min.padStart(2, "0")}` : null;
}

/** A single-entry schedule moved to `time` ("HH:MM"), its days kept (several entries are never collapsed to one time). */
export const withCronTime = (cron: readonly string[], time: string): string[] => {
  const [h, m] = time.split(":").map(Number);
  const [, , ...rest] = (cron[0] ?? "").trim().split(/\s+/);
  return [[m, h, ...rest].join(" ")];
};

export { budgetLine, deliveryWords, money, runNowReason, runsPerMonth, scheduleWords, viewerTimeZone, waitingWords } from "@lightbulb/shared";

/** "Finance" for an automation's specialist: an agent id by its name (the live provider's agent names). */
export function useSpecialistLabel(): (specialistId: string | null | undefined) => string {
  const { agentNames } = useLiveEvents();
  return useCallback((id) => specialistLabel(id, agentNames), [agentNames]);
}

/** Where an automation posts, naming the room the viewer knows it as: "posts to #walk-8" (dev walk W8). */
export function useDeliveryWords(): (deliverTo: string) => string | null {
  const rooms = useMessagingWorkspaceOptional()?.directoryProps.rooms;
  return useCallback((deliverTo) => {
    const id = deliveryRoomId(deliverTo);
    const room = id ? rooms?.find((r) => r.roomId === id) : undefined;
    const name = room ? roomDisplayName(room, room.participants) : null;
    return deliveryWords(deliverTo, name ?? null);
  }, [rooms]);
}
