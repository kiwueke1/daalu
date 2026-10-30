/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone",
  async rewrites() {
    const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
    return [
      // Proxy API + SSE traffic so the browser sees a single origin and
      // the SSE connection survives without CORS preflights.
      { source: "/api/:path*", destination: `${apiBase}/api/:path*` },
      { source: "/sse/:path*", destination: `${apiBase}/api/:path*` },
      // The browser-IDE proxy lives on the backend at /ide/... (not under
      // /api). In prod Traefik routes /ide straight to daalu-api; this
      // rewrite gives the same single-origin behaviour in local dev so
      // "Open IDE" doesn't 404 against the Next.js dev server.
      { source: "/ide/:path*", destination: `${apiBase}/ide/:path*` },
      // Device SSH terminal WebSocket (topology UI → asyncssh PTY). Same
      // single-origin story as /ide — in prod Traefik routes /ws to daalu-api.
      { source: "/ws/:path*", destination: `${apiBase}/ws/:path*` },
    ];
  },
};

module.exports = nextConfig;
