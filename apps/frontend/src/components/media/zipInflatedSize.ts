const EOCD = 0x06054b50;
const CDH = 0x02014b50;
const ZIP64 = 0xffffffff;

/**
 * The total uncompressed size a zip's central directory declares, read without inflating
 * anything. null when the directory is missing or malformed, or any entry is ZIP64.
 */
export function zipInflatedSize(buf: ArrayBuffer): number | null {
  const v = new DataView(buf);
  let eocd = -1;
  // The EOCD record is 22 bytes plus a comment of up to 65,535.
  for (let i = buf.byteLength - 22, stop = Math.max(0, buf.byteLength - 65_557); i >= stop; i--) {
    if (v.getUint32(i, true) === EOCD) { eocd = i; break; }
  }
  if (eocd < 0) return null;
  const count = v.getUint16(eocd + 10, true);
  const size = v.getUint32(eocd + 12, true);
  let at = v.getUint32(eocd + 16, true);
  if (count === 0xffff || size === ZIP64 || at === ZIP64 || at + size > eocd) return null;
  let total = 0;
  for (let n = 0; n < count; n++) {
    if (at + 46 > eocd || v.getUint32(at, true) !== CDH) return null;
    const compressed = v.getUint32(at + 20, true);
    const inflated = v.getUint32(at + 24, true);
    if (compressed === ZIP64 || inflated === ZIP64) return null;
    total += inflated;
    at += 46 + v.getUint16(at + 28, true) + v.getUint16(at + 30, true) + v.getUint16(at + 32, true);
  }
  return total;
}
