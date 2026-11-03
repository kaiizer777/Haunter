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

    // Check actual file line numbers starting from hunk header (10, 11, 12)
    expect(screen.getByText("10")).toBeInTheDocument();
    expect(screen.getAllByText("11")).toHaveLength(2);
    expect(screen.getByText("12")).toBeInTheDocument();

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
    expect(screen.queryByText("10")).not.toBeInTheDocument();
  });

  it("formats replacement snippets cleanly and strips markdown code fences", () => {
    const fencedSnippet = "```ts\nconst token = authHeader.split(' ')[1];\nreturn verifyToken(token);\n```";
    render(<DiffViewer patch={fencedSnippet} />);

    expect(
      screen.getByText("const token = authHeader.split(' ')[1];")
    ).toBeInTheDocument();
    expect(
      screen.getByText("return verifyToken(token);")
    ).toBeInTheDocument();
    // Fences stripped
    expect(screen.queryByText("```ts")).not.toBeInTheDocument();

    // Default line numbers start at 1, 2 for plain replacement snippets
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("preserves leading indentation on line 1 for replacement snippets", () => {
    const indentedSnippet = "  const indented = true;\n  return indented;";
    const { container } = render(<DiffViewer patch={indentedSnippet} />);

    const lines = container.querySelectorAll(".tracking-tight");
    expect(lines[0].textContent).toBe("  const indented = true;");
    expect(lines[1].textContent).toBe("  return indented;");
  });

  it("distinguishes git file headers from code lines starting with +++ or ---", () => {
    const patchWithCode = `--- a/src/counter.ts
+++ b/src/counter.ts
@@ -1,4 +1,5 @@
 const base = 10;
+---count;
++++count;
+let next = 1;`;

    const { container } = render(<DiffViewer patch={patchWithCode} />);

    // File headers should be rendered with zinc styling and no line number
    const headers = container.querySelectorAll(".border-zinc-600");
    expect(headers).toHaveLength(2);
    expect(screen.getByText("--- a/src/counter.ts")).toBeInTheDocument();
    expect(screen.getByText("+++ b/src/counter.ts")).toBeInTheDocument();

    // Changed code lines inside hunk starting with +++ or --- should be treated as code additions
    const additionLines = container.querySelectorAll(".border-emerald-500");
    expect(additionLines).toHaveLength(3);
    expect(screen.getByText("+---count;")).toBeInTheDocument();
    expect(screen.getByText("++++count;")).toBeInTheDocument();
  });

  it("handles empty or blank patch gracefully", () => {
    render(<DiffViewer patch="   " />);

    expect(
      screen.getByText("No patch content available")
    ).toBeInTheDocument();
  });
});
