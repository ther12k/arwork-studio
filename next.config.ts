import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // Type errors must fail the build: a working preview never substitutes
  // for type checks (review finding). Keep this OFF.
  reactStrictMode: false,
};

export default nextConfig;
