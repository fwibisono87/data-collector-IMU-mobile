import { execSync } from "node:child_process";

/**
 * Identity of THIS bundle, resolved at build time.
 *
 * The dashboard compares this against the backend's own commit (GET /health -> build_id) so a
 * skew is caught in preflight, before a session, instead of surfacing as a render crash at save.
 * On 2026-08-11 a browser ran a bundle that existed nowhere on disk and matched no commit; there
 * was no way to notice until it threw.
 *
 * Falls back to "unknown" rather than inventing a value — the preflight check treats "unknown"
 * as pending, never as a mismatch, so a build outside a git checkout cannot raise a false alarm.
 */
function resolveBuildId() {
  if (process.env.BUILD_ID) return process.env.BUILD_ID;
  try {
    return execSync("git rev-parse --short HEAD", { stdio: ["ignore", "pipe", "ignore"] })
      .toString()
      .trim();
  } catch {
    return "unknown";
  }
}

const BUILD_ID = resolveBuildId();

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,

  generateBuildId: async () => BUILD_ID,

  // Exposed to client code as process.env.NEXT_PUBLIC_BUILD_ID.
  env: {
    NEXT_PUBLIC_BUILD_ID: BUILD_ID,
  },

  async headers() {
    return [
      {
        // Build assets are content-hashed, so their names change whenever their contents do.
        // They are safe — and important — to cache aggressively.
        source: "/_next/static/:path*",
        headers: [{ key: "Cache-Control", value: "public, max-age=31536000, immutable" }],
      },
      {
        // Everything else, and above all the HTML document, must be revalidated. A cached
        // document pins the browser to a chunk graph that may no longer exist on the server:
        // that is exactly how the 2026-08-11 dashboard ended up executing a bundle from an
        // older deploy against a newer backend. The negative lookahead keeps the immutable
        // rule above from being overridden by this one.
        source: "/((?!_next/static).*)",
        headers: [{ key: "Cache-Control", value: "no-store, must-revalidate" }],
      },
    ];
  },
};

export default nextConfig;
