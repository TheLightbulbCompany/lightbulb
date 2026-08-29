import schedule from "../../../../launch-schedule.json";

export interface LaunchScheduleEnv { drop_at: string | null; competition_start_at: string | null }
export interface LaunchScheduleFile {
  competition_days: number;
  comp_slots: number;
  pot_front_cents: number;
  pot_per_entrant_cents: number;
  dev: LaunchScheduleEnv;
  prod: LaunchScheduleEnv;
}

/** The one schedule file, importable at build time for static copy (dates, pot constants). Live phase still comes from GET /launch. */
export const LAUNCH_SCHEDULE = schedule as unknown as LaunchScheduleFile;

export function launchDatesFor(env: "dev" | "prod"): LaunchScheduleEnv {
  return LAUNCH_SCHEDULE[env];
}
