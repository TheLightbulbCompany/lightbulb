"use client";

import { useState } from "react";

import { EditorialButton } from "@/components/teams/shell";
import { useApi } from "@/lib/api";
import { notifyError } from "@/lib/notify";
import { MISSING_WORKFLOW_MSG } from "@/components/teams/inbox/ProvisioningConsentActions";
import { payloadString } from "@/components/teams/inbox/lib/approvalLabel";
import type { Approval } from "@/components/teams/shared/types";

/**
 * The provider's own JSON Schema, rendered by a CONSTRAINED renderer.
 *
 * Only the five shapes the backend already refuses to file a card for anything
 * outside of (`provisioning_workflow._renderable`): enum, string, number,
 * integer, boolean. Anything else is not a form we can be sure we are asking
 * for correctly, so it says so and offers no submit — a half-rendered form
 * would submit a half-answer to the provider.
 *
 * The schema on the card is descriptive only: the backend validates the answer
 * against the schema IT stored, never one the browser hands back.
 */

type Field = {
  name: string;
  kind: "enum" | "string" | "number" | "boolean";
  options: string[];
  required: boolean;
};

const NUMERIC = new Set(["number", "integer"]);

/** `null` when the schema holds a shape this renderer cannot draw. */
export function readFields(schema: unknown): Field[] | null {
  const properties = (schema as { properties?: unknown } | null)?.properties;
  if (!properties || typeof properties !== "object") return null;
  const requiredRaw = (schema as { required?: unknown }).required;
  const required = new Set(
    Array.isArray(requiredRaw) ? requiredRaw.filter((r): r is string => typeof r === "string") : [],
  );
  const fields: Field[] = [];
  for (const [name, raw] of Object.entries(properties as Record<string, unknown>)) {
    if (!raw || typeof raw !== "object") return null;
    const spec = raw as { type?: unknown; enum?: unknown };
    if (Array.isArray(spec.enum)) {
      fields.push({
        name,
        kind: "enum",
        options: spec.enum.map((o) => String(o)),
        required: required.has(name),
      });
      continue;
    }
    const type = typeof spec.type === "string" ? spec.type : "";
    if (type === "string") fields.push({ name, kind: "string", options: [], required: required.has(name) });
    else if (NUMERIC.has(type)) fields.push({ name, kind: "number", options: [], required: required.has(name) });
    else if (type === "boolean") fields.push({ name, kind: "boolean", options: [], required: required.has(name) });
    else return null;
  }
  return fields.length > 0 ? fields : null;
}

export function ProvisioningInformationForm({
  approval,
  busy,
  run,
  approve,
}: {
  approval: Approval;
  busy: boolean;
  run: (fn: () => Promise<void>) => void;
  approve: (approvalId: string) => Promise<void>;
}) {
  const api = useApi();
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const [fieldError, setFieldError] = useState<string | null>(null);
  const payload = approval.payload;
  const provider = payloadString(payload, "provider");
  const workflowId = payloadString(payload, "workflow_id");
  const fields = readFields(payload?.schema);

  if (!fields) {
    return (
      <p className="pl-6 text-sm text-[var(--midnight-muted)]">
        {provider} is asking for something this form needs Lightbulb to handle — email support and
        we&apos;ll take it from here.
      </p>
    );
  }

  /** A checkbox always carries an answer; everything else needs typing. */
  function answered(field: Field): boolean {
    const value = values[field.name];
    return field.kind === "boolean" ? true : typeof value === "string" && value !== "";
  }

  function answer(field: Field): string | number | boolean | undefined {
    const value = values[field.name];
    if (field.kind === "boolean") {
      // An untouched optional checkbox is left OUT rather than sent as a
      // `false` the owner never chose.
      if (value === undefined && !field.required) return undefined;
      return value === true;
    }
    const text = typeof value === "string" ? value : "";
    // Never coerced: `Number("")` is 0, and 0 is an answer nobody gave. An
    // empty required field cannot get here — Send stays disabled — and an
    // empty optional one is left out, since the provider's schema decides
    // what an absent answer means.
    if (text === "") return undefined;
    return field.kind === "number" ? Number(text) : text;
  }

  function submit(answering: Field[]) {
    setFieldError(null);
    // Same guard as the consent card: with no id there is nothing to submit
    // to, and approving anyway would tell the owner the provider has their
    // answers when nothing was ever sent.
    if (!workflowId) {
      notifyError(MISSING_WORKFLOW_MSG);
      return;
    }
    run(async () => {
      const information: Record<string, string | number | boolean> = {};
      for (const field of answering) {
        const value = answer(field);
        if (value !== undefined) information[field.name] = value;
      }
      try {
        await api.post(`/company/provisioning/workflows/${workflowId}/information`, { information });
      } catch (e) {
        // 422 names the field the provider's own schema rejected — that
        // belongs beside the form, not in a toast that hides which one.
        if ((e as { status?: number }).status === 422) {
          setFieldError((e as Error).message);
          return;
        }
        throw e;
      }
      await approve(approval.id);
    });
  }

  return (
    <div className="flex flex-col gap-2 pl-6 text-sm">
      {fields.map((field) => {
        const id = `info-${approval.id}-${field.name}`;
        return (
          <span key={field.name} className="flex items-center gap-2">
            <label htmlFor={id} className="text-[var(--midnight-muted)]">
              {field.name}
            </label>
            {field.required && (
              <span aria-hidden="true" className="text-[var(--err)]">
                *
              </span>
            )}
            {field.kind === "enum" ? (
              <select
                id={id}
                required={field.required}
                disabled={busy}
                value={typeof values[field.name] === "string" ? (values[field.name] as string) : ""}
                onChange={(e) => setValues((v) => ({ ...v, [field.name]: e.target.value }))}
                className="bg-transparent text-[var(--midnight)] outline-none"
                style={{ borderBottom: "1px solid var(--edge)" }}
              >
                <option value="">Choose…</option>
                {field.options.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={id}
                type={field.kind === "boolean" ? "checkbox" : field.kind === "number" ? "number" : "text"}
                required={field.required}
                disabled={busy}
                checked={field.kind === "boolean" ? values[field.name] === true : undefined}
                value={
                  field.kind === "boolean"
                    ? undefined
                    : typeof values[field.name] === "string"
                      ? (values[field.name] as string)
                      : ""
                }
                onChange={(e) =>
                  setValues((v) => ({
                    ...v,
                    [field.name]: field.kind === "boolean" ? e.target.checked : e.target.value,
                  }))
                }
                className="bg-transparent text-[var(--midnight)] outline-none"
                style={field.kind === "boolean" ? undefined : { borderBottom: "1px solid var(--edge)" }}
              />
            )}
          </span>
        );
      })}
      {fieldError && <p className="text-sm text-[var(--err)]">{fieldError}</p>}
      <span>
        <EditorialButton
          size="sm"
          disabled={busy || fields.some((field) => field.required && !answered(field))}
          onClick={() => submit(fields)}
        >
          {`Send to ${provider}`}
        </EditorialButton>
      </span>
    </div>
  );
}
