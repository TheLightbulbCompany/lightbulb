import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PanelDataTable, type PanelDataColumn } from "@/components/teams/shell/PanelDataTable";

interface Row {
  id: string;
  name: string;
  count: number;
}

const ROWS: Row[] = [
  { id: "1", name: "Alpha", count: 3 },
  { id: "2", name: "Bravo", count: 1 },
];

const COLUMNS: PanelDataColumn<Row>[] = [
  { key: "name", header: "Name", cell: (row) => row.name, sortable: true },
  { key: "count", header: "Count", cell: (row) => row.count, sortable: true },
];

describe("PanelDataTable", () => {
  it("renders rows + columns", () => {
    render(<PanelDataTable<Row> rows={ROWS} columns={COLUMNS} rowKey={(r) => r.id} />);
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("Bravo")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /name/i })).toBeInTheDocument();
  });

  it("calls onSortChange when a sortable header is clicked (asc -> desc)", async () => {
    const onSortChange = vi.fn();
    render(
      <PanelDataTable<Row>
        rows={ROWS}
        columns={COLUMNS}
        rowKey={(r) => r.id}
        sortKey="name"
        sortDir="asc"
        onSortChange={onSortChange}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: /name/i }));
    expect(onSortChange).toHaveBeenCalledWith("name", "desc");
  });

  it("calls onSortChange asc when clicking a non-active sortable header", async () => {
    const onSortChange = vi.fn();
    render(
      <PanelDataTable<Row>
        rows={ROWS}
        columns={COLUMNS}
        rowKey={(r) => r.id}
        sortKey="name"
        sortDir="asc"
        onSortChange={onSortChange}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: /count/i }));
    expect(onSortChange).toHaveBeenCalledWith("count", "asc");
  });

  it("renders empty content when rows is empty", () => {
    render(
      <PanelDataTable<Row>
        rows={[]}
        columns={COLUMNS}
        rowKey={(r) => r.id}
        empty={<div>nothing here</div>}
      />
    );
    expect(screen.getByText("nothing here")).toBeInTheDocument();
  });

  it("calls onRowClick when a row is clicked", async () => {
    const onRowClick = vi.fn();
    render(
      <PanelDataTable<Row>
        rows={ROWS}
        columns={COLUMNS}
        rowKey={(r) => r.id}
        onRowClick={onRowClick}
      />
    );
    await userEvent.click(screen.getByText("Alpha"));
    expect(onRowClick).toHaveBeenCalledWith(ROWS[0]);
  });

  it("does not render sort buttons for non-sortable columns", () => {
    const cols: PanelDataColumn<Row>[] = [
      { key: "name", header: "Name", cell: (r) => r.name }, // sortable not set
      { key: "count", header: "Count", cell: (r) => r.count, sortable: true },
    ];
    render(
      <PanelDataTable<Row>
        rows={ROWS}
        columns={cols}
        rowKey={(r) => r.id}
        onSortChange={vi.fn()}
        sortKey="count"
        sortDir="asc"
      />
    );
    // Only "Count" should be a button; "Name" is plain text
    expect(screen.getByRole("button", { name: /count/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /name/i })).not.toBeInTheDocument();
  });
});
