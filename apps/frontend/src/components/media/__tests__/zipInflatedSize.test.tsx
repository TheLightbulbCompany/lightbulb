import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import * as XLSX from "xlsx";
import { zipInflatedSize } from "../zipInflatedSize";

vi.mock("xlsx", async (orig) => {
  const real = await orig<typeof import("xlsx")>();
  return { ...real, read: vi.fn(real.read) };
});

import { SheetGrid, SHEET_MAX_INFLATED_BYTES } from "../SheetGrid";

const XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

/** One central directory entry claiming `inflated` bytes, then the EOCD record. */
function fakeZip(inflated: number): ArrayBuffer {
  const name = 6; // "a.xml" plus one
  const buf = new ArrayBuffer(4 + 46 + name + 22);
  const v = new DataView(buf);
  v.setUint32(0, 0x04034b50, true); // "PK" local header magic
  const cd = 4;
  v.setUint32(cd, 0x02014b50, true);
  v.setUint32(cd + 20, 1000, true);
  v.setUint32(cd + 24, inflated, true);
  v.setUint16(cd + 28, name, true);
  const eocd = cd + 46 + name;
  v.setUint32(eocd, 0x06054b50, true);
  v.setUint16(eocd + 8, 1, true);
  v.setUint16(eocd + 10, 1, true);
  v.setUint32(eocd + 12, 46 + name, true);
  v.setUint32(eocd + 16, cd, true);
  return buf;
}

it("sums a real workbook's declared sizes, under the cap", () => {
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([["a", "b"], [1, 2]]), "S");
  const n = zipInflatedSize(XLSX.write(wb, { type: "array", bookType: "xlsx" }) as ArrayBuffer);
  expect(n).toBeGreaterThan(0);
  expect(n!).toBeLessThan(SHEET_MAX_INFLATED_BYTES);
});

it("refuses ZIP64 markers and bytes with no central directory", () => {
  expect(zipInflatedSize(fakeZip(0xffffffff))).toBeNull();
  expect(zipInflatedSize(new Uint8Array([0x50, 0x4b, 3, 4, 0, 0]).buffer)).toBeNull();
  expect(zipInflatedSize(fakeZip(4096))).toBe(4096);
});

it("shows the fallback card for a workbook declaring 4 GiB, without calling read", async () => {
  vi.mocked(XLSX.read).mockClear();
  const blob = new Blob([fakeZip(0xfffffffe)], { type: XLSX_TYPE });
  render(<SheetGrid blob={blob} fileName="bomb.xlsx" csv={false} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not preview bomb.xlsx. Download it instead.");
  expect(XLSX.read).not.toHaveBeenCalled();
});
