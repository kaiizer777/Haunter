import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

process.env.NEXT_PUBLIC_API_URL = process.env.NEXT_PUBLIC_API_URL || "https://api.example.com";

afterEach(() => {
  cleanup();
});
