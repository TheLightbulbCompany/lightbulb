import { describe, expect, it } from "vitest";
import { PERSON_COLORS, personColor } from "./personColor";

describe("personColor", () => {
  it("matches the terminal mod's colorFor (rules.ts) for the same Clerk ids", () => {
    // Values computed with apps/desktop/src-tauri/mods/lightbulb-sessions/hooks/rules.ts colorFor.
    expect(personColor("user_alice", "owner")).toBe("magenta");
    expect(personColor("user_bob", "owner")).toBe("green");
    expect(personColor("user_carol", "owner")).toBe("cyan");
    expect(personColor("user_dave", "owner")).toBe("blueBright");
    expect(personColor("user_erin", "owner")).toBe("yellow");
    expect(personColor("user_2abcDEF", "owner")).toBe("blue");
    expect(PERSON_COLORS).toEqual(["cyan", "magenta", "green", "yellow", "blue", "blueBright"]);
  });
  it("gives the session's own person the owner colour", () => {
    expect(personColor("user_alice", "user_alice")).toBe("owner");
  });
});
