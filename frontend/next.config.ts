import type { NextConfig } from "next";

// The browser calls /api/*, which Next forwards to the Gazi API, so there's no cross-origin setup.
const apiUrl = process.env.GAZI_API_URL ?? "http://localhost:8000";

const config: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/:path*` }];
  },
};

export default config;
