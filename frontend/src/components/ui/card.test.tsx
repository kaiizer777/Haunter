import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  CardFooter,
} from "./card";

describe("card.tsx", () => {
  it("renders a complete Card structure with all subcomponents", () => {
    render(
      <Card data-testid="card-root">
        <CardHeader data-testid="card-header">
          <CardTitle data-testid="card-title">Test Title</CardTitle>
          <CardDescription data-testid="card-desc">Test Description</CardDescription>
        </CardHeader>
        <CardContent data-testid="card-content">
          <p>Main body content</p>
        </CardContent>
        <CardFooter data-testid="card-footer">
          <button>Action</button>
        </CardFooter>
      </Card>
    );

    const root = screen.getByTestId("card-root");
    expect(root).toBeInTheDocument();
    expect(root).toHaveClass("rounded-[6px]", "border", "border-zinc-800", "bg-[#121215]");

    const header = screen.getByTestId("card-header");
    expect(header).toBeInTheDocument();
    expect(header).toHaveClass("flex", "flex-col", "p-4");

    const title = screen.getByTestId("card-title");
    expect(title).toBeInTheDocument();
    expect(title.tagName).toBe("H3");
    expect(title).toHaveClass("text-sm", "font-semibold", "text-zinc-100");
    expect(title).toHaveTextContent("Test Title");

    const desc = screen.getByTestId("card-desc");
    expect(desc).toBeInTheDocument();
    expect(desc.tagName).toBe("P");
    expect(desc).toHaveClass("text-xs", "text-zinc-400");
    expect(desc).toHaveTextContent("Test Description");

    const content = screen.getByTestId("card-content");
    expect(content).toBeInTheDocument();
    expect(content).toHaveClass("p-4");
    expect(content).toHaveTextContent("Main body content");

    const footer = screen.getByTestId("card-footer");
    expect(footer).toBeInTheDocument();
    expect(footer).toHaveClass("flex", "items-center", "border-t");
    expect(footer).toHaveTextContent("Action");
  });

  it("forwards custom classNames to each subcomponent", () => {
    render(
      <Card className="custom-card">
        <CardHeader className="custom-header">
          <CardTitle className="custom-title">Title</CardTitle>
          <CardDescription className="custom-desc">Desc</CardDescription>
        </CardHeader>
        <CardContent className="custom-content">Body</CardContent>
        <CardFooter className="custom-footer">Footer</CardFooter>
      </Card>
    );

    expect(screen.getByText("Title").closest(".custom-card")).toBeInTheDocument();
    expect(screen.getByText("Title").parentElement).toHaveClass("custom-header");
    expect(screen.getByText("Title")).toHaveClass("custom-title");
    expect(screen.getByText("Desc")).toHaveClass("custom-desc");
    expect(screen.getByText("Body")).toHaveClass("custom-content");
    expect(screen.getByText("Footer")).toHaveClass("custom-footer");
  });

  it("forwards ref to Card and CardTitle elements", () => {
    const cardRef = React.createRef<HTMLDivElement>();
    const titleRef = React.createRef<HTMLHeadingElement>();
    render(
      <Card ref={cardRef}>
        <CardTitle ref={titleRef}>Referenced</CardTitle>
      </Card>
    );

    expect(cardRef.current).toBeInstanceOf(HTMLDivElement);
    expect(titleRef.current).toBeInstanceOf(HTMLHeadingElement);
  });
});
