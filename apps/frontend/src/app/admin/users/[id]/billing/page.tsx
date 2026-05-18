import { auth } from "@clerk/nextjs/server";

import { DefRow } from "@/components/admin/DefRow";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { asNumber, asRecord, asString } from "@/app/admin/_lib/narrow";
import { getOverview } from "@/app/admin/_lib/api";

import { BillingActionsPanel } from "./BillingActionsPanel";

export const metadata = { title: "Billing \u00b7 Admin" };

interface PageProps {
  params: Promise<{ id: string }>;
}

// ---------------------------------------------------------------------------
// Local narrowing of the (intentionally permissive) overview slices.
// ---------------------------------------------------------------------------

interface BillingSlice {
  subscription_status?: string | null;
  stripe_customer_id?: string;
  stripe_subscription_id?: string;
  current_period_end?: string;
  current_invoice?: {
    id?: string;
    amount_due_cents?: number;
    status?: string;
    hosted_invoice_url?: string;
  };
  error?: string;
  source?: string;
}

interface IdentitySlice {
  email_addresses?: Array<{ email_address?: string }>;
  primary_email_address_id?: string;
}

function pickBilling(raw: unknown): BillingSlice {
  const r = asRecord(raw);
  const inv = asRecord(r.current_invoice);
  return {
    stripe_customer_id: asString(r.stripe_customer_id),
    stripe_subscription_id: asString(r.stripe_subscription_id),
    subscription_status: asString(r.subscription_status) ?? asString(r.status),
    current_period_end: asString(r.current_period_end),
    current_invoice:
      Object.keys(inv).length === 0
        ? undefined
        : {
            id: asString(inv.id),
            amount_due_cents: asNumber(inv.amount_due_cents),
            status: asString(inv.status),
            hosted_invoice_url: asString(inv.hosted_invoice_url),
          },
    error: asString(r.error),
    source: asString(r.source),
  };
}

function pickIdentityEmail(raw: unknown): string | null {
  const r = asRecord(raw) as IdentitySlice;
  const list = r.email_addresses ?? [];
  if (!Array.isArray(list) || list.length === 0) return null;
  const first = list[0];
  return asString(first?.email_address) ?? null;
}

function formatCents(cents: number | undefined): string {
  if (cents === undefined) return "\u2014";
  const dollars = (cents / 100).toFixed(2);
  return `$${dollars}`;
}

export default async function AdminUserBillingPage({ params }: PageProps) {
  const { id } = await params;

  const { getToken } = await auth();
  const token = await getToken();
  const overview = token ? await getOverview(token, id) : null;

  if (!overview) {
    return (
      <div className="space-y-6">
        <Header />
        <ErrorBanner error="Billing overview unreachable" variant="error" />
      </div>
    );
  }

  const billing = pickBilling(overview.billing);
  const email = pickIdentityEmail(overview.identity);
  const billingError = billing.error;

  return (
    <div className="space-y-6">
      <Header />

      {billingError ? (
        <ErrorBanner
          error={billingError}
          source={billing.source ?? "Stripe"}
          variant="error"
        />
      ) : null}

      {/* Billing card */}
      <section className="space-y-3 rounded-md border border-white/10 bg-white/[0.02] p-4">
        <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-400">
          Subscription
        </h2>
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          <DefRow label="Subscription" value={billing.subscription_status ?? "\u2014"} />
          <DefRow
            label="Status"
            value={billing.subscription_status ?? "\u2014"}
          />
          <DefRow
            label="Stripe customer"
            value={billing.stripe_customer_id ?? "\u2014"}
            mono
            href={
              billing.stripe_customer_id
                ? `https://dashboard.stripe.com/customers/${encodeURIComponent(billing.stripe_customer_id)}`
                : undefined
            }
          />
          <DefRow
            label="Stripe subscription"
            value={billing.stripe_subscription_id ?? "\u2014"}
            mono
            href={
              billing.stripe_subscription_id
                ? `https://dashboard.stripe.com/subscriptions/${encodeURIComponent(billing.stripe_subscription_id)}`
                : undefined
            }
          />
          <DefRow
            label="Period end"
            value={billing.current_period_end ?? "\u2014"}
            mono
          />
        </dl>
      </section>

      {/* Current invoice */}
      <section className="space-y-3 rounded-md border border-white/10 bg-white/[0.02] p-4">
        <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-400">
          Current invoice
        </h2>
        {billing.current_invoice ? (
          <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            <DefRow
              label="Invoice ID"
              value={billing.current_invoice.id ?? "\u2014"}
              mono
              href={billing.current_invoice.hosted_invoice_url}
            />
            <DefRow
              label="Status"
              value={billing.current_invoice.status ?? "\u2014"}
            />
            <DefRow
              label="Amount due"
              value={formatCents(billing.current_invoice.amount_due_cents)}
              mono
            />
          </dl>
        ) : (
          <p className="text-sm text-zinc-500">No active invoice.</p>
        )}
      </section>

      {/* Actions */}
      <BillingActionsPanel userId={id} email={email} />
    </div>
  );
}

// User-id breadcrumb is provided by the parent layout — render only the
// section heading here so the title doesn't duplicate.
function Header() {
  return <h1 className="text-xl font-semibold text-zinc-100">Billing</h1>;
}

