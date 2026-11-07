import { describe, expect, it } from "vitest";
import { FORWARDING_CODES, forwardingCodes } from "../forwarding";

const N = "4842079294";

describe("forwarding codes", () => {
  it("verizon missed = *71, att all = **21*<n>#, google voice offers all only", () => {
    expect(forwardingCodes("verizon", "missed", N)).toEqual({ on: [`*71${N}`], off: ["*73"] });
    expect(forwardingCodes("att", "all", N)).toEqual({ on: [`**21*${N}#`], off: ["##21#"] });
    expect(forwardingCodes("tmobile", "missed", N)?.on).toEqual([`**61*${N}#`, `**67*${N}#`]);
    expect(FORWARDING_CODES.google_voice.modes).toEqual(["all"]);
    expect(forwardingCodes("google_voice", "missed", N)).toBeNull();
    expect(forwardingCodes("voip", "missed", N)).toBeNull(); // set in the admin portal, no code
  });
});
