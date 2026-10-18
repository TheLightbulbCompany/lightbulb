"use client";

import { useEffect, useState } from "react";
import { blobBytes } from "./readBlob";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

export const SHEET_MAX_ROWS = 2_000;
export const SHEET_MAX_BYTES = 5 * 1024 * 1024;

type Sheet = { name: string; header: string[]; rows: string[][]; total: number };
type State = { status: "loading" } | { status: "ready"; sheets: Sheet[] } | { status: "error" };

const text = (v: unknown) => (v === null || v === undefined ? "" : String(v));

/** A workbook or CSV parsed in the browser with SheetJS: one tab per sheet, the first 2,000 rows each. View only. */
export function SheetGrid({ blob, fileName }: { blob: Blob; fileName: string }) {
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
        const head = new Uint8Array(bytes, 0, 2);
        if (blob.type !== "text/csv" && !(head[0] === 0x50 && head[1] === 0x4b)) throw new Error("not a workbook");
        const wb = read(bytes, { type: "array", dense: true });
        const sheets = wb.SheetNames.map((name) => {
          const aoa = utils.sheet_to_json<unknown[]>(wb.Sheets[name], { header: 1, blankrows: false }) as unknown[][];
          const [head = [], ...body] = aoa;
          return { name, header: head.map(text), rows: body.slice(0, SHEET_MAX_ROWS).map((r) => r.map(text)), total: body.length };
        });
        if (!sheets.length) throw new Error("empty");
        if (live) setState({ status: "ready", sheets });
      } catch {
        if (live) setState({ status: "error" });
      }
    })();
    return () => { live = false; };
  }, [blob]);
  if (state.status === "loading") return <p className="p-4 text-ui text-muted-foreground">Loading {fileName}…</p>;
  if (state.status === "error") return <p role="alert" className="p-4 text-ui text-muted-foreground">Could not preview {fileName}. Download it instead.</p>;
  const sheet = state.sheets[Math.min(active, state.sheets.length - 1)];
  const width = Math.max(sheet.header.length, ...sheet.rows.map((r) => r.length));
  return (
    <div className="flex h-full min-h-0 flex-col">
      {state.sheets.length > 1 && (
        <Tabs value={String(active)} onValueChange={(v) => setActive(Number(v))}>
          <TabsList>{state.sheets.map((s, i) => <TabsTrigger key={s.name} value={String(i)}>{s.name}</TabsTrigger>)}</TabsList>
        </Tabs>
      )}
      <div className="min-h-0 flex-1 overflow-auto">
        <Table>
          <TableHeader><TableRow>{Array.from({ length: width }, (_, c) => <TableHead key={c}>{sheet.header[c] ?? ""}</TableHead>)}</TableRow></TableHeader>
          <TableBody>{sheet.rows.map((r, i) => <TableRow key={i}>{Array.from({ length: width }, (_, c) => <TableCell key={c}>{r[c] ?? ""}</TableCell>)}</TableRow>)}</TableBody>
        </Table>
      </div>
      {sheet.total > SHEET_MAX_ROWS && <p className="p-2 text-caption text-muted-foreground">Showing the first {SHEET_MAX_ROWS.toLocaleString()} of {sheet.total.toLocaleString()} rows.</p>}
    </div>
  );
}
