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
  /** When true (and there are no rows), render quiet skeleton rows in place
   * of the table body. Takes precedence over `empty`. */
  loading?: boolean;
  /** When set (and there are no rows), render an error row spanning all
   * columns. Takes precedence over `loading` and `empty`. */
  error?: ReactNode;
  /** Number of skeleton rows to render while `loading`. Defaults to 3. */
  loadingRows?: number;
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
  loading = false,
  error,
  loadingRows = 3,
  className,
}: PanelDataTableProps<Row>) {
  const isEmpty = rows.length === 0;
  // Error and loading affordances only stand in when there's nothing to show —
  // a populated table keeps rendering its rows even while revalidating.
  if (isEmpty && error != null) {
    return (
      <div
        role="alert"
        className="px-3 py-2 text-ui"
        style={{ color: "var(--err)" }}
      >
        {error}
      </div>
    );
  }
  if (isEmpty && loading) {
    return (
      <Table className={className} aria-busy="true">
        <TableHeader>
          <TableRow className="border-b border-[var(--edge)]">
            {columns.map((col) => (
              <TableHead
                key={col.key}
                style={col.width ? { width: col.width } : undefined}
                className={cn(
                  col.align === "right" && "text-right",
                  col.align === "center" && "text-center",
                )}
              >
                <span className="label-caps">{col.header}</span>
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {Array.from({ length: loadingRows }).map((_, rowIndex) => (
            <TableRow
              key={`skeleton-${rowIndex}`}
              className="border-b border-[var(--edge)]"
            >
              {columns.map((col) => (
                <TableCell key={col.key} className="py-3">
                  <span
                    aria-hidden="true"
                    className="block h-3 animate-pulse rounded-sm bg-[var(--cream-deep)]"
                  />
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    );
  }
  if (isEmpty && empty) {
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
              aria-sort={
                col.sortable
                  ? sortKey === col.key
                    ? sortDir === "asc"
                      ? "ascending"
                      : "descending"
                    : "none"
                  : undefined
              }
              className={cn(
                col.align === "right" && "text-right",
                col.align === "center" && "text-center",
              )}
            >
              {col.sortable && onSortChange ? (
                <button
                  type="button"
                  onClick={() => toggleSort(col)}
                  className="label-caps inline-flex items-center gap-1 transition-colors duration-(--dur-fast) ease-out hover:text-[var(--midnight)]"
                >
                  {col.header}
                  {sortKey === col.key &&
                    (sortDir === "asc" ? (
                      <ChevronUp size={12} strokeWidth={1.75} aria-hidden="true" />
                    ) : (
                      <ChevronDown size={12} strokeWidth={1.75} aria-hidden="true" />
                    ))}
                </button>
              ) : (
                <span className="label-caps">{col.header}</span>
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
            role={onRowClick ? "button" : undefined}
            tabIndex={onRowClick ? 0 : undefined}
            onKeyDown={
              onRowClick
                ? (event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onRowClick(row);
                    }
                  }
                : undefined
            }
            className={cn(
              "border-b border-[var(--edge)] transition-colors",
              onRowClick &&
                "cursor-pointer hover:bg-[var(--edge)]",
            )}
          >
            {columns.map((col, colIndex) => (
              <TableCell
                key={col.key}
                className={cn(
                  "text-ui text-[var(--midnight)]",
                  // Primary column: a long issue/agent title used to widen the
                  // column until the trailing metadata columns were pushed out
                  // of the panel. Cap it and ellipsize instead.
                  colIndex === 0 && !col.width && "max-w-[42ch] truncate",
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
