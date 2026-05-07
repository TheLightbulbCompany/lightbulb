"use client";

import type { ReactNode } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

export interface PanelDataColumn<Row> {
  key: string;
  header: ReactNode;
  cell: (row: Row) => ReactNode;
  sortable?: boolean;
  align?: "left" | "right" | "center";
  width?: string;
}

export type PanelDataSortDir = "asc" | "desc";

export interface PanelDataTableProps<Row> {
  rows: Row[];
  columns: PanelDataColumn<Row>[];
  rowKey: (row: Row) => string;
  onRowClick?: (row: Row) => void;
  sortKey?: string;
  sortDir?: PanelDataSortDir;
  onSortChange?: (key: string, dir: PanelDataSortDir) => void;
  empty?: ReactNode;
  className?: string;
}

export function PanelDataTable<Row>({
  rows,
  columns,
  rowKey,
  onRowClick,
  sortKey,
  sortDir,
  onSortChange,
  empty,
  className,
}: PanelDataTableProps<Row>) {
  if (rows.length === 0 && empty) {
    return <div className="px-3 py-2">{empty}</div>;
  }

  function toggleSort(col: PanelDataColumn<Row>) {
    if (!col.sortable || !onSortChange) return;
    const nextDir: PanelDataSortDir =
      sortKey === col.key && sortDir === "asc" ? "desc" : "asc";
    onSortChange(col.key, nextDir);
  }

  return (
    <Table className={className}>
      <TableHeader>
        <TableRow className="border-b border-[var(--edge)]">
          {columns.map((col) => (
            <TableHead
              key={col.key}
              style={col.width ? { width: col.width } : undefined}
              className={cn(
                "text-[10px] font-semibold uppercase tracking-[0.12em]",
                col.align === "right" && "text-right",
                col.align === "center" && "text-center",
              )}
            >
              {col.sortable && onSortChange ? (
                <button
                  type="button"
                  onClick={() => toggleSort(col)}
                  className="inline-flex items-center gap-1 text-[var(--mute)] transition-colors hover:text-[var(--ink)]"
                >
                  {col.header}
                  {sortKey === col.key &&
                    (sortDir === "asc" ? (
                      <ChevronUp aria-hidden="true" className="h-3 w-3" />
                    ) : (
                      <ChevronDown aria-hidden="true" className="h-3 w-3" />
                    ))}
                </button>
              ) : (
                <span className="text-[var(--mute)]">{col.header}</span>
              )}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow
            key={rowKey(row)}
            onClick={onRowClick ? () => onRowClick(row) : undefined}
            className={cn(
              "border-b border-[var(--edge)] transition-colors",
              onRowClick && "cursor-pointer hover:bg-[var(--edge)]",
            )}
          >
            {columns.map((col) => (
              <TableCell
                key={col.key}
                className={cn(
                  "text-[13px] text-[var(--ink)]",
                  col.align === "right" && "text-right",
                  col.align === "center" && "text-center",
                )}
              >
                {col.cell(row)}
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
