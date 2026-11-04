import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone output = a self-contained server bundle, so the Docker runtime
  // image ships without node_modules.
  output: "standalone",
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
