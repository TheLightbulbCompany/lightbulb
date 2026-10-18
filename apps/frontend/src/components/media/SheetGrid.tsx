"use client";

import { useEffect, useState } from "react";
import { blobBytes } from "./readBlob";
import { zipInflatedSize } from "./zipInflatedSize";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

export const SHEET_MAX_ROWS = 2_000;
export const SHEET_MAX_BYTES = 5 * 1024 * 1024;
export const SHEET_MAX_COLS = 200;
/** read() inflates every worksheet before sheetRows can cut, so a zip bomb under 5 MiB is refused here. */
export const SHEET_MAX_INFLATED_BYTES = 64 * 1024 * 1024;

type Sheet = { name: string; header: string[]; rows: string[][]; total: number; cols: number };
type State = { status: "loading" } | { status: "ready"; sheets: Sheet[] } | { status: "error" };

// ponytail: counts newlines, so a quoted cell with a line break overcounts; parse fully if that matters.
function csvRows(b: Uint8Array): number {
  let n = 0;
  for (let i = 0; i < b.length; i++) if (b[i] === 0x0a) n++;
  if (b.length && b[b.length - 1] !== 0x0a) n++;
  return n - 1; // the header line
}

const text = (v: unknown) => (v === null || v === undefined ? "" : String(v));

/** A workbook or CSV parsed in the browser with SheetJS: one tab per sheet, the first 2,000 rows each. View only. */
export function SheetGrid({ blob, fileName, csv }: { blob: Blob; fileName: string; csv: boolean }) {
  const [state, setState] = useState<State>({ status: "loading" });
  const [active, setActive] = useState(0);
  useEffect(() => {
    let live = true;
    (async () => {
      try {
        if (blob.size > SHEET_MAX_BYTES) throw new Error("too large");
        const { read, utils } = await import("xlsx");
        const bytes = await blobBytes(blob);
        // SheetJS reads any bytes as text, so garbage "parses"; a workbook is a zip ("PK"), only CSV may be bare text.
        const head = new Uint8Array(bytes.slice(0, 2));
        if (!csv && !(head[0] === 0x50 && head[1] === 0x4b)) throw new Error("not a workbook");
        if (!csv) {
          const inflated = zipInflatedSize(bytes);
          if (inflated === null || inflated > SHEET_MAX_INFLATED_BYTES) throw new Error("too large inflated");
        }
        // sheetRows stops the parse at the header plus 2,000 rows; "!fullref" keeps the real extent.
        const wb = read(bytes, { type: "array", dense: true, sheetRows: SHEET_MAX_ROWS + 1 });
        const sheets = wb.SheetNames.map((name) => {
          const ws = wb.Sheets[name];
          const aoa = utils.sheet_to_json<unknown[]>(ws, { header: 1, blankrows: false }) as unknown[][];
          const [head = [], ...body] = aoa;
          // A workbook keeps its real extent in "!fullref"; a CSV parse does not, so count its lines.
          const full = ws["!fullref"];
          const total = full ? utils.decode_range(full).e.r - utils.decode_range(full).s.r
            : csv && body.length >= SHEET_MAX_ROWS ? csvRows(new Uint8Array(bytes)) : body.length;
          const cols = Math.max(head.length, ...body.map((r) => r.length));
          const cut = (r: unknown[]) => r.slice(0, SHEET_MAX_COLS).map(text);
          return { name, header: cut(head), rows: body.slice(0, SHEET_MAX_ROWS).map(cut), total, cols };
        });
        if (!sheets.length) throw new Error("empty");
        if (live) setState({ status: "ready", sheets });
      } catch {
        if (live) setState({ status: "error" });
      }
    })();
    return () => { live = false; };
  }, [blob, csv]);
  if (state.status === "loading") return <p className="p-4 text-ui text-muted-foreground">Loading {fileName}…</p>;
  if (state.status === "error") return <p role="alert" className="p-4 text-ui text-muted-foreground">Could not preview {fileName}. Download it instead.</p>;
  const sheet = state.sheets[Math.min(active, state.sheets.length - 1)];
  const width = Math.min(sheet.cols, SHEET_MAX_COLS);
  return (
    <div className="flex h-full min-h-0 flex-col">
      {state.sheets.length > 1 && (
        <Tabs value={String(active)} onValueChange={(v) => setActive(Number(v))}>
          <TabsList className="max-w-full justify-start overflow-x-auto">{state.sheets.map((s, i) => <TabsTrigger key={s.name} value={String(i)} className="flex-none">{s.name}</TabsTrigger>)}</TabsList>
        </Tabs>
      )}
      <div className="min-h-0 flex-1 overflow-auto">
        <Table>
          <TableHeader><TableRow>{Array.from({ length: width }, (_, c) => <TableHead key={c}>{sheet.header[c] ?? ""}</TableHead>)}</TableRow></TableHeader>
          <TableBody>{sheet.rows.map((r, i) => <TableRow key={i}>{Array.from({ length: width }, (_, c) => <TableCell key={c}>{r[c] ?? ""}</TableCell>)}</TableRow>)}</TableBody>
        </Table>
      </div>
      {sheet.cols > SHEET_MAX_COLS && <p className="p-2 text-caption text-muted-foreground">Showing the first {SHEET_MAX_COLS} of {sheet.cols.toLocaleString()} columns.</p>}
      {sheet.total > SHEET_MAX_ROWS && <p className="p-2 text-caption text-muted-foreground">Showing the first {SHEET_MAX_ROWS.toLocaleString()} of {sheet.total.toLocaleString()} rows.</p>}
    </div>
  );
}
