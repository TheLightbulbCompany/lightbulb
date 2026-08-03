import { describe, expect, it } from "vitest";
import { splitCollidedHeaders } from "../MessageList";

describe("splitCollidedHeaders", () => {
  it("breaks colliding reasoning-summary headers into paragraphs", () => {
    expect(splitCollidedHeaders("**One****Two****Three**")).toBe("**One**\n\n**Two**\n\n**Three**");
  });

  it("leaves already-separated headers alone", () => {
    const md = "**One**\n\nbody\n\n**Two**";
    expect(splitCollidedHeaders(md)).toBe(md);
  });

  it("leaves a lone bold span alone", () => {
    expect(splitCollidedHeaders("some **bold** text")).toBe("some **bold** text");
  });
});
