const DEFAULT_DURATION_MS = 24 * 60 * 60 * 1000; // 24 hours

export function isSnoozed(key: string): boolean {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return false;
    const snoozedUntil = Number(raw);
    if (Date.now() < snoozedUntil) return true;
    localStorage.removeItem(key);
    return false;
  } catch {
    return false;
  }
}

export function setSnoozed(key: string, durationMs = DEFAULT_DURATION_MS): void {
  localStorage.setItem(key, String(Date.now() + durationMs));
}

export function budgetSnoozeKey(threshold: number): string {
  return `lightbulb_budget_snooze_${threshold}`;
}

export function updateSnoozeKey(updateId: string): string {
  return `lightbulb_update_snooze_${updateId}`;
}
