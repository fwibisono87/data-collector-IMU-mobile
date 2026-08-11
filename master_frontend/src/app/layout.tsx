import type { Metadata } from "next";
import { GeistSans } from "geist/font/sans";
import "./globals.css";

export const metadata: Metadata = {
  title: "IMU Telemetry Dashboard",
  description: "Operator dashboard for IMU data collection sessions",
};

/**
 * Serve the document per request rather than as a static prerender.
 *
 * Next.js stamps its own `Cache-Control: s-maxage=..., stale-while-revalidate` on statically
 * prerendered pages, and that wins over both `headers()` in next.config.mjs and middleware —
 * both were tried against a running `next start` and neither took effect. Making the segment
 * dynamic is what actually lets the no-store header through.
 *
 * The cost is one server render per dashboard load, which is nothing for a single-operator LAN
 * tool. What it buys: the browser can never pin itself to a cached document referencing a chunk
 * graph the server no longer has — the 2026-08-11 failure, where a bundle that existed on no
 * disk and matched no commit ran against a newer backend and crashed at save.
 */
export const dynamic = "force-dynamic";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={GeistSans.className}>
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
