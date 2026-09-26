import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
  devIndicators: false,
  env: {
    NEXT_PUBLIC_API_URL:
      process.env.NEXT_PUBLIC_API_URL || "http://localhost:7555",
  },
};

// COOP/COEP required by @webcontainer/api (SharedArrayBuffer).
// `headers()` only applies to `next dev`; under `output: "export"` Next emits
// a config warning AND silently ignores it, so we gate the rule to dev only.
// Production headers are served by Cloudflare Workers Static Assets via
// public/_headers (see https://developers.cloudflare.com/workers/static-assets/headers/).
if (process.env.NODE_ENV !== "production") {
  nextConfig.headers = async () => [
    {
      source: "/(.*)",
      headers: [
        { key: "Cross-Origin-Embedder-Policy", value: "require-corp" },
        { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
      ],
    },
  ];
}

export default nextConfig;
