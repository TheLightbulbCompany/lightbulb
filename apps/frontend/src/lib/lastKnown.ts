// What this browser last saw for one person in one workspace, so a reload can draw the app at once while the
// server confirms (W12-18). Only booleans that are safe to show early: the server's answer replaces them in a beat.
// Private mode or blocked storage reads as "unknown", which keeps the old wait.

const PREFIX = "lightbulb.lastKnown";

export function readLastKnown(name: string, scope: string): boolean | null {
  try {
    const value = localStorage.getItem(`${PREFIX}.${name}:${scope}`);
    return value === "1" ? true : value === "0" ? false : null;
  } catch {
    return null;
  }
}

export function writeLastKnown(name: string, scope: string, value: boolean): void {
  try {
    localStorage.setItem(`${PREFIX}.${name}:${scope}`, value ? "1" : "0");
  } catch {
    /* storage blocked: the next load waits as before */
  }
}
