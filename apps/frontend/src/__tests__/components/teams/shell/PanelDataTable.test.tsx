import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import {
  PanelDataTable,
  type PanelDataColumn,
} from "@/components/teams/shell/PanelDataTable";

interface Row {
  id: string;
  name: string;
}

const columns: PanelDataColumn<Row>[] = [
  { key: "name", header: "Name", cell: (r) => r.name },
  { key: "id", header: "ID", cell: (r) => r.id },
];

const rows: Row[] = [
  { id: "a", name: "Alpha" },
  { id: "b", name: "Beta" },
];

function rowKey(r: Row) {
  return r.id;
}

describe("PanelDataTable", () => {
  it("renders rows when present", () => {
    const { getByText } = render(
      <PanelDataTable rows={rows} columns={columns} rowKey={rowKey} />,
    );
    expect(getByText("Alpha")).toBeInTheDocument();
    expect(getByText("Beta")).toBeInTheDocument();
  });

  it("renders the empty branch when there are no rows", () => {
    const { getByText } = render(
      <PanelDataTable
        rows={[]}
        columns={columns}
        rowKey={rowKey}
        empty={<span>Nothing here</span>}
      />,
    );
    expect(getByText("Nothing here")).toBeInTheDocument();
  });

  it("renders an error row (role=alert) when error is set and rows are empty", () => {
    const { getByRole, getByText } = render(
      <PanelDataTable
        rows={[]}
        columns={columns}
        rowKey={rowKey}
        error="Failed to load"
        empty={<span>Nothing here</span>}
      />,
    );
    const alert = getByRole("alert");
    expect(alert).toHaveTextContent("Failed to load");
    // Error takes precedence over the empty branch.
    expect(() => getByText("Nothing here")).toThrow();
  });

  it("renders skeleton rows when loading and rows are empty", () => {
    const { container, queryByText } = render(
      <PanelDataTable
        rows={[]}
        columns={columns}
        rowKey={rowKey}
        loading
        loadingRows={4}
        empty={<span>Nothing here</span>}
      />,
    );
    const table = container.querySelector('[aria-busy="true"]');
    expect(table).not.toBeNull();
    // 4 skeleton body rows (header row is in <thead>).
    expect(container.querySelectorAll("tbody tr")).toHaveLength(4);
    // Loading takes precedence over the empty branch.
    expect(queryByText("Nothing here")).toBeNull();
  });

  it("error takes precedence over loading", () => {
    const { getByRole, container } = render(
      <PanelDataTable
        rows={[]}
        columns={columns}
        rowKey={rowKey}
        loading
        error="Boom"
      />,
    );
    expect(getByRole("alert")).toHaveTextContent("Boom");
    expect(container.querySelector('[aria-busy="true"]')).toBeNull();
  });

  it("keeps rendering rows even when loading/error are set (non-empty wins)", () => {
    const { getByText, queryByRole } = render(
      <PanelDataTable
        rows={rows}
        columns={columns}
        rowKey={rowKey}
        loading
        error="Boom"
      />,
    );
    expect(getByText("Alpha")).toBeInTheDocument();
    expect(queryByRole("alert")).toBeNull();
  });

  it("makes clickable rows keyboard-operable (role=button, tabIndex, Enter/Space)", () => {
    const onRowClick = vi.fn();
    const { getAllByRole } = render(
      <PanelDataTable
        rows={rows}
        columns={columns}
        rowKey={rowKey}
        onRowClick={onRowClick}
      />,
    );
    const buttons = getAllByRole("button");
    expect(buttons).toHaveLength(2);
    expect(buttons[0]).toHaveAttribute("tabindex", "0");

    fireEvent.keyDown(buttons[0], { key: "Enter" });
    expect(onRowClick).toHaveBeenCalledWith(rows[0]);

    fireEvent.keyDown(buttons[1], { key: " " });
    expect(onRowClick).toHaveBeenCalledWith(rows[1]);
    expect(onRowClick).toHaveBeenCalledTimes(2);
  });

  it("does not expose role=button when onRowClick is absent", () => {
    const { queryAllByRole } = render(
      <PanelDataTable rows={rows} columns={columns} rowKey={rowKey} />,
    );
    expect(queryAllByRole("button")).toHaveLength(0);
  });
});
