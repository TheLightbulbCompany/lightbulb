"use client";

import { useState } from "react";

import { EditorialSelect } from "@/components/teams/shell";
import {
  buildCron,
  describeCron,
  parseCron,
  type Schedule,
} from "@/components/teams/shared/cron-readable";

// "When should this run?" in the surface's own language — frequency plus a
// time, not `0 9 * * *`. The cron expression stays the single source of truth
// (it is what the backend stores), so this is a VIEW over one: `parseCron`
// opens the picker on an existing schedule, `buildCron` writes the edits back,
// and `describeCron` — the same formatter the automations list uses — renders
// the confirmation line, which is what keeps a picker-built schedule reading
// identically once it lands in the list.
//
// An expression outside the picker's vocabulary (day-of-month, steps, lists)
// opens in the raw field instead. Rewriting it into the nearest expressible
// shape would silently change when someone's automation fires, so cron stays
// available as the escape hatch rather than the default.

const FREQ_OPTIONS: ReadonlyArray<{ value: Schedule["freq"]; label: string }> = [
  { value: "hourly", label: "Every hour" },
  { value: "daily", label: "Every day" },
  { value: "weekdays", label: "Every weekday" },
  { value: "weekly", label: "Every week" },
];

const DAY_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: "1", label: "Monday" },
  { value: "2", label: "Tuesday" },
  { value: "3", label: "Wednesday" },
  { value: "4", label: "Thursday" },
  { value: "5", label: "Friday" },
  { value: "6", label: "Saturday" },
  { value: "0", label: "Sunday" },
];

const DEFAULT_SCHEDULE: Schedule = { freq: "daily", hour: 9, minute: 0, day: 1 };

const INPUT_CLASS =
  "w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 text-[var(--midnight)]";

function toTimeValue(s: Schedule): string {
  return `${String(s.hour).padStart(2, "0")}:${String(s.minute).padStart(2, "0")}`;
}

export function ScheduleField({
  value,
  onChange,
}: {
  /** Cron expression — the stored value this field reads and writes. */
  value: string;
  onChange: (cron: string) => void;
}) {
  // Advanced mode is latched on mount for an expression the picker can't hold,
  // so reopening someone's hand-written cron never silently normalizes it.
  const [advanced, setAdvanced] = useState(() => parseCron(value) === null);
  const schedule = parseCron(value) ?? DEFAULT_SCHEDULE;

  function update(next: Partial<Schedule>) {
    onChange(buildCron({ ...schedule, ...next }));
  }

  if (advanced) {
    return (
      <div className="block">
        <span className="text-ui text-[var(--midnight-soft)]">Schedule</span>
        <input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="0 9 * * *"
          aria-label="Cron expression"
          className={`${INPUT_CLASS} mt-1 font-mono`}
        />
        <div className="flex items-center justify-between gap-2">
          {/* describeCron echoes the expression back when it can't read it —
              show the field's purpose instead of the same string twice. */}
          <span className="text-caption text-[var(--midnight-muted)]">
            {describeCron(value) === value.trim()
              ? "Cron expression — when the automation fires."
              : describeCron(value)}
          </span>
          <button
            type="button"
            onClick={() => {
              // Falling back to the picker on an unrepresentable expression
              // would rewrite it, so seed the default and let the user see the
              // change happen instead.
              if (parseCron(value) === null) onChange(buildCron(DEFAULT_SCHEDULE));
              setAdvanced(false);
            }}
            className="shrink-0 text-caption text-[var(--midnight)] hover:underline"
          >
            Use the picker
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="block">
      <span className="text-ui text-[var(--midnight-soft)]">Schedule</span>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <EditorialSelect
          className="w-[150px]"
          options={FREQ_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
          value={schedule.freq}
          onValueChange={(next) => update({ freq: next as Schedule["freq"] })}
        />
        {schedule.freq === "weekly" && (
          <EditorialSelect
            className="w-[130px]"
            options={[...DAY_OPTIONS]}
            value={String(schedule.day)}
            onValueChange={(next) => update({ day: Number(next) })}
          />
        )}
        <input
          type="time"
          aria-label={schedule.freq === "hourly" ? "Minutes past the hour" : "Time of day"}
          value={toTimeValue(schedule)}
          onChange={(e) => {
            const [h, m] = e.target.value.split(":").map(Number);
            if (Number.isFinite(h) && Number.isFinite(m)) update({ hour: h, minute: m });
          }}
          className="w-[120px] border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 text-[var(--midnight)]"
        />
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className="text-caption text-[var(--midnight-muted)]">{describeCron(value)}</span>
        <button
          type="button"
          onClick={() => setAdvanced(true)}
          className="shrink-0 text-caption text-[var(--midnight)] hover:underline"
        >
          Use a cron expression
        </button>
      </div>
    </div>
  );
}
