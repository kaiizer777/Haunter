import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
  devIndicators: false,
  env: {
    NEXT_PUBLIC_API_URL:
      process.env.NEXT_PUBLIC_API_URL || "http://localhost:7555",
  },
  // COOP/COEP required by @webcontainer/api (SharedArrayBuffer).
  // `headers()` only applies to `next dev` — `output: "export"` ignores it.
  // Production headers are served by Cloudflare Workers Static Assets via
  // out/_headers (authored in public/_headers; see
  // https://developers.cloudflare.com/workers/static-assets/headers/).
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "Cross-Origin-Embedder-Policy", value: "require-corp" },
          { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
