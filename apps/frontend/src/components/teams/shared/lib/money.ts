// apps/frontend/src/components/teams/shared/lib/money.ts

// Shared cents → USD formatter for the Teams surface (DashboardPanel has a
// local copy; new call sites should import this one). Renders e.g.
// `formatCents(1999)` → "$19.99".

export function formatCents(cents: number): string {
  return `$${(cents / 100).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}
