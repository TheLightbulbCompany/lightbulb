"use client";

import { Input } from "@/components/ui/input";
import { capture } from "@/lib/analytics";
import { SOURCES } from "@/components/requestSetup/options";

/** "How did you first hear about us?": the setup form's list (T22), one list everywhere.
 *  Optional on the workspace form (D13). */
export const SOURCE_OPTIONS = SOURCES;

export type SourceAnswer = { answer: string; detail: string };

/** `source_reported`, with the answer also kept on the person (`$set_once`). No-op unanswered. */
export function reportSource(surface: "sign_up" | "setup_form", { answer, detail }: SourceAnswer, extra?: Record<string, unknown>) {
  if (!answer) return;
  capture("source_reported", {
    surface,
    answer,
    detail: detail.trim() || null,
    ...extra,
    $set_once: { self_reported_source: answer },
  });
}

export function SourceQuestion({
  value,
  onChange,
  required = false,
}: {
  value: SourceAnswer;
  onChange: (next: SourceAnswer) => void;
  required?: boolean;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label className="flex flex-col gap-1.5 text-sm font-medium">
        How did you first hear about us?{required ? "" : <span className="font-normal text-muted-foreground"> (optional)</span>}
        <select
          className="h-9 rounded-md border border-border bg-background px-3 text-sm"
          value={value.answer}
          required={required}
          onChange={(e) => onChange({ ...value, answer: e.target.value })}
        >
          <option value="">Choose one</option>
          {SOURCE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </label>
      {value.answer && (
        <Input
          aria-label="Tell us more"
          placeholder="Tell us more (optional)"
          value={value.detail}
          maxLength={500}
          onChange={(e) => onChange({ ...value, detail: e.target.value })}
        />
      )}
    </div>
  );
}
