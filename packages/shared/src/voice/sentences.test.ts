// sentences.test.ts
import { describe, expect, it } from "vitest";
import { takeSentences } from "./sentences";

describe("takeSentences", () => {
  it("returns complete sentences and keeps the tail", () => {
    expect(takeSentences("It landed. Four thousand two")).toEqual({ ready: "It landed.", rest: " Four thousand two" });
  });
  it("keeps everything when no sentence has ended", () => {
    expect(takeSentences("Checking the")).toEqual({ ready: "", rest: "Checking the" });
  });
  it("does not split decimals", () => {
    expect(takeSentences("It is $4.21 today. Next")).toEqual({ ready: "It is $4.21 today.", rest: " Next" });
  });
});
