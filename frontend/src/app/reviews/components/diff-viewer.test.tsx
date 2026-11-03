import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { DiffViewer } from "./diff-viewer";

describe("DiffViewer component", () => {
  const samplePatch = `@@ -10,4 +10,5 @@
 const timeout = 5000;
-const retry = false;
+const retry = true;
+const maxRetries = 3;`;

  it("renders syntax-highlighted diff with line numbers", () => {
    const { container } = render(<DiffViewer patch={samplePatch} />);

    expect(screen.getByText("@@ -10,4 +10,5 @@")).toBeInTheDocument();
    expect(screen.getByText("-const retry = false;")).toBeInTheDocument();
    expect(screen.getByText("+const retry = true;")).toBeInTheDocument();
    expect(screen.getByText("+const maxRetries = 3;")).toBeInTheDocument();

    // Check line numbers 1 to 5
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();

    // Line styles: chunk header has cyan classes
    const chunkLine = container.querySelector(".border-cyan-500");
    expect(chunkLine).toBeInTheDocument();

    // Addition line has emerald classes
    const additionLine = container.querySelector(".border-emerald-500");
    expect(additionLine).toBeInTheDocument();

    // Deletion line has rose classes
    const deletionLine = container.querySelector(".border-rose-500");
    expect(deletionLine).toBeInTheDocument();
  });

  it("renders header with filename and additions/deletions counts", () => {
    render(
      <DiffViewer
        patch={samplePatch}
        filename="src/utils/retry-handler.ts"
      />
    );

    const header = screen.getByTestId("diff-header");
    expect(header).toBeInTheDocument();
    expect(within(header).getByText("src/utils/retry-handler.ts")).toBeInTheDocument();

    // 2 additions (+), 1 deletion (-)
    expect(screen.getByTestId("diff-additions")).toHaveTextContent("2");
    expect(screen.getByTestId("diff-deletions")).toHaveTextContent("1");
  });

  it("hides line numbers when showLineNumbers is false", () => {
    render(<DiffViewer patch={samplePatch} showLineNumbers={false} />);

    expect(screen.getByText("-const retry = false;")).toBeInTheDocument();
    expect(screen.queryByText("1")).not.toBeInTheDocument();
  });

  it("handles empty or blank patch gracefully", () => {
    render(<DiffViewer patch="   " />);

    expect(
      screen.getByText("No patch content available")
    ).toBeInTheDocument();
  });
});
