"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { Card } from "@/components/admin/Card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

import { saveLaunchEmailCopy, sendLaunchTestEmail } from "../_actions/launch";
import type { LaunchEmailVariant } from "../_lib/api";

type Result = Awaited<ReturnType<typeof saveLaunchEmailCopy>>;

const LABELS: Record<string, string> = {
  join: "Join confirmation",
  winner_paid: "Winner — already paying",
  winner_unpaid: "Winner — not yet subscribed",
  competing: "Not a winner, still competing",
  not_selected: "Not selected",
};

/** 422s ride the same `{detail: {errors: {field: message}}}` envelope as the
 * settings PUT. */
function fieldErrorsFrom(result: Result): Record<string, string> | null {
  if (result.status !== 422 || !result.data || typeof result.data !== "object") return null;
  const detail = (result.data as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object" || !("errors" in detail)) return null;
  return (detail as { errors?: Record<string, string> }).errors ?? null;
}

function describeError(result: Result): string {
  const errors = fieldErrorsFrom(result);
  if (errors) {
    return Object.entries(errors)
      .map(([field, msg]) => `${field}: ${msg}`)
      .join("; ");
  }
  return result.error ?? `http_${result.status}`;
}

function VariantBlock({ v }: { v: LaunchEmailVariant }) {
  const router = useRouter();
  // Prefilled with the EFFECTIVE copy — the override where there is one, the
  // shipped default otherwise, so the editor always shows what customers get.
  const [subject, setSubject] = React.useState(v.override.subject ?? v.defaults.subject);
  const [lead, setLead] = React.useState(v.override.lead ?? v.defaults.lead);
  const [detail, setDetail] = React.useState(v.override.detail ?? v.defaults.detail);
  const [busy, setBusy] = React.useState(false);
  const [note, setNote] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const overridden = Object.keys(v.override).length > 0;

  async function save(fields: { subject: string; lead: string; detail: string }) {
    setBusy(true);
    setNote(null);
    setError(null);
    const r = await saveLaunchEmailCopy({ variant: v.variant, ...fields });
    setBusy(false);
    if (r.ok) {
      setNote("Saved.");
      router.refresh();
    } else {
      setError(describeError(r));
    }
  }

  async function reset() {
    setSubject(v.defaults.subject);
    setLead(v.defaults.lead);
    setDetail(v.defaults.detail);
    // Blank on the wire: the row stops overriding and the shipped copy is back.
    await save({ subject: "", lead: "", detail: "" });
  }

  async function sendTest() {
    setBusy(true);
    setNote(null);
    setError(null);
    const r = await sendLaunchTestEmail(v.variant);
    setBusy(false);
    if (r.ok) {
      const to = (r.data as { sent_to?: string } | undefined)?.sent_to ?? "your admin address";
      setNote(`Test sent to ${to}.`);
    } else {
      setError(describeError(r));
    }
  }

  return (
    <details className="rounded-md border border-border px-3 py-2">
      <summary className="cursor-pointer text-sm text-foreground">
        {LABELS[v.variant] ?? v.variant}{" "}
        <span className="text-xs text-muted-foreground">
          {overridden ? "· edited" : "· default"}
        </span>
      </summary>
      <div className="mt-3 space-y-3">
        <label className="block space-y-1">
          <span className="text-xs text-muted-foreground">Subject</span>
          <Input value={subject} onChange={(e) => setSubject(e.target.value)} disabled={busy} />
        </label>
        <label className="block space-y-1">
          <span className="text-xs text-muted-foreground">Lead (the bold first line)</span>
          <Textarea rows={2} value={lead} onChange={(e) => setLead(e.target.value)} disabled={busy} />
        </label>
        <label className="block space-y-1">
          <span className="text-xs text-muted-foreground">Detail</span>
          <Textarea rows={5} value={detail} onChange={(e) => setDetail(e.target.value)} disabled={busy} />
        </label>
        <p className="text-xs text-muted-foreground">
          Plain text — HTML is escaped, never rendered.{" "}
          {v.placeholders.length > 0
            ? `Placeholders: ${v.placeholders.map((p) => `{${p}}`).join(", ")}.`
            : "This email supports no placeholders."}
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" disabled={busy} onClick={() => save({ subject, lead, detail })}>
            Save
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={reset}>
            Reset to default
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={sendTest}>
            Send test to me
          </Button>
          {note ? <span className="text-xs text-muted-foreground">{note}</span> : null}
          {error ? <span className="text-xs text-destructive">{error}</span> : null}
        </div>
        <div className="space-y-1">
          <p className="text-xs text-muted-foreground">
            Preview (saved copy, sample values) — subject: {v.preview_subject}
          </p>
          {/* An iframe, not dangerouslySetInnerHTML: the email body is rendered
              in its own sandboxed document, so it can neither inherit the
              console's CSS nor run anything in it. The white ground lives
              INSIDE that document (the backend's `preview_document`), like a
              real mail client's — the email body hardcodes a dark text
              colour, so a theme token here would make the preview unreadable
              in dark mode. */}
          <iframe
            title={`${v.variant} preview`}
            sandbox=""
            srcDoc={v.preview_html}
            className="h-56 w-full rounded border border-border"
          />
        </div>
      </div>
    </details>
  );
}

/**
 * The admin editor for the launch emails' copy. The shipped strings live in
 * the backend (`core/services/launch_sheet_email.py`) and stay the source of
 * truth; this only writes OVERRIDES onto the launch-settings row, which is
 * why "Reset to default" saves blanks rather than the default text.
 */
export function LaunchEmailsCard({ variants }: { variants: LaunchEmailVariant[] }) {
  return (
    <Card title="Launch emails">
      <p className="mb-3 text-xs text-muted-foreground">
        The copy every sheet member gets. Blank a field to fall back to the shipped default.
      </p>
      <div className="space-y-2">
        {variants.map((v) => (
          <VariantBlock key={v.variant} v={v} />
        ))}
      </div>
    </Card>
  );
}
