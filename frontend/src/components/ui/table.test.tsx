import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableRow,
  TableHead,
  TableCell,
  TableCaption,
} from "./table";

describe("table.tsx", () => {
  it("renders a full semantic table with all subcomponents", () => {
    render(
      <Table data-testid="table-root">
        <TableCaption>Haunter Runs Overview</TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead>Run ID</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Latency</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow data-testid="table-row-1">
            <TableCell>run_001</TableCell>
            <TableCell>PR Opened</TableCell>
            <TableCell>14.2s</TableCell>
          </TableRow>
        </TableBody>
        <TableFooter>
          <TableRow>
            <TableCell colSpan={2}>Total</TableCell>
            <TableCell>14.2s</TableCell>
          </TableRow>
        </TableFooter>
      </Table>
    );

    const table = screen.getByRole("table");
    expect(table).toBeInTheDocument();
    expect(table).toHaveClass("w-full", "caption-bottom", "text-xs");

    expect(screen.getByText("Haunter Runs Overview")).toBeInTheDocument();
    expect(screen.getByText("Haunter Runs Overview").tagName).toBe("CAPTION");

    const header = screen.getByText("Run ID").closest("thead");
    expect(header).toBeInTheDocument();
    expect(header?.tagName).toBe("THEAD");

    const th = screen.getByText("Run ID");
    expect(th.tagName).toBe("TH");
    expect(th).toHaveClass("h-9", "px-3", "text-zinc-400");

    const row = screen.getByTestId("table-row-1");
    expect(row.tagName).toBe("TR");

    const td = screen.getByText("run_001");
    expect(td.tagName).toBe("TD");
    expect(td).toHaveClass("p-3", "text-zinc-200");

    const footer = screen.getByText("Total").closest("tfoot");
    expect(footer).toBeInTheDocument();
    expect(footer?.tagName).toBe("TFOOT");
  });

  it("forwards custom classNames to all elements", () => {
    render(
      <Table className="custom-table">
        <TableHeader className="custom-header">
          <TableRow className="custom-row">
            <TableHead className="custom-th">Header</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody className="custom-body">
          <TableRow>
            <TableCell className="custom-td">Cell</TableCell>
          </TableRow>
        </TableBody>
        <TableFooter className="custom-footer">
          <TableRow>
            <TableCell>Footer</TableCell>
          </TableRow>
        </TableFooter>
        <TableCaption className="custom-caption">Caption</TableCaption>
      </Table>
    );

    expect(screen.getByRole("table")).toHaveClass("custom-table");
    expect(screen.getByText("Header").closest("thead")).toHaveClass("custom-header");
    expect(screen.getByText("Header")).toHaveClass("custom-th");
    expect(screen.getByText("Cell").closest("tbody")).toHaveClass("custom-body");
    expect(screen.getByText("Cell")).toHaveClass("custom-td");
    expect(screen.getByText("Footer").closest("tfoot")).toHaveClass("custom-footer");
    expect(screen.getByText("Caption")).toHaveClass("custom-caption");
  });

  it("forwards refs to Table and TableCell", () => {
    const tableRef = React.createRef<HTMLTableElement>();
    const cellRef = React.createRef<HTMLTableCellElement>();

    render(
      <Table ref={tableRef}>
        <TableBody>
          <TableRow>
            <TableCell ref={cellRef}>Ref Cell</TableCell>
          </TableRow>
        </TableBody>
      </Table>
    );

    expect(tableRef.current).toBeInstanceOf(HTMLTableElement);
    expect(cellRef.current).toBeInstanceOf(HTMLTableCellElement);
  });
});
