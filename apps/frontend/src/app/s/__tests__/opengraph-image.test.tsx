// @vitest-environment node
import { writeFileSync } from "node:fs";
import { expect, it } from "vitest";
import SessionLinkCard, { size } from "../opengraph-image";

it("draws the session link's social card as a PNG", async () => {
  const png = Buffer.from(await SessionLinkCard().arrayBuffer());
  expect(png.subarray(1, 4).toString()).toBe("PNG");
  expect(png.readUInt32BE(16)).toBe(size.width);
  expect(png.readUInt32BE(20)).toBe(size.height);
  if (process.env.OG_OUT) writeFileSync(process.env.OG_OUT, png);
});
