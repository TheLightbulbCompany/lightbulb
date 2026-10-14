import { clsx, type ClassValue } from "clsx"
import { extendTailwindMerge } from "tailwind-merge"

// Our @theme type steps (globals.css). Unregistered, twMerge reads
// `text-ui` as a colour and drops it when a colour class follows it.
const twMerge = extendTailwindMerge({
  extend: { classGroups: { "font-size": [{ text: ["2xs", "caption", "ui", "title"] }] } },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Clip `value` to `max` characters, appending an ellipsis when it clips.
 *
 * `max` bounds the text BEFORE the ellipsis, so the returned string can be
 * `max + 1` characters. A caller that needs the ellipsis to fit inside a hard
 * budget passes `max - 1`.
 *
 * Trailing whitespace is trimmed off the clip so a cut landing mid-space
 * renders `"foo…"` rather than `"foo …"`.
 */
export function truncate(value: string, max: number): string {
  if (value.length <= max) return value
  return `${value.slice(0, max).trimEnd()}…`
}

/**
 * Human-readable byte size, scaling B -> KB -> MB -> GB.
 *
 * Whole bytes render without a decimal (`"512 B"`); everything above shows one
 * (`"1.5 MB"`). Non-finite and non-positive input renders `"0 B"` rather than
 * `"NaN B"` — sizes reach here from API payloads where a missing field is
 * routine.
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B"
  const units = ["B", "KB", "MB", "GB"]
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)))
  const value = bytes / 1024 ** i
  return `${i === 0 ? value : value.toFixed(1)} ${units[i]}`
}
