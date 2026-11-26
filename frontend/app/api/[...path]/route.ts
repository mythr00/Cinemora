/**
 * Server-side API proxy (F5 deployment; Phase 8.5 session model).
 *
 * In deployed builds the browser client runs in "same-origin" mode
 * (NEXT_PUBLIC_API_BASE_URL=same-origin): every API call goes to
 * this origin's /api/* routes, i.e. here. This handler forwards
 * the request to the real backend carrying the visitor's OWN
 * session cookie — Cinemora is multi-user now (Phase 8): the
 * browser signs in at /login, the backend issues an httpOnly
 * session cookie, and this proxy forwards that cookie so every
 * request runs as the signed-in user and only ever sees that
 * user's data (the backend enforces ownership; cross-tenant reads
 * are 404 there).
 *
 * The pre-8.5 behaviour — injecting one shared operator token
 * server-side — is retired: a shared token would make every
 * visitor the same principal and defeat tenant isolation. A
 * browser-supplied X-API-Key header is therefore stripped, never
 * forwarded (the client no longer sends one; stripping is defence
 * in depth against stale bundles).
 *
 * Server-only configuration (never NEXT_PUBLIC_*):
 * - CINEMORA_BACKEND_URL: origin of the Cinemora backend.
 */

import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const BACKEND_URL = (process.env.CINEMORA_BACKEND_URL || "").replace(
  /\/$/,
  "",
);

const FORWARDED_RESPONSE_HEADERS = [
  "content-type",
  "content-disposition",
  "content-length",
  "cache-control",
  "etag",
  "last-modified",
  // Media streaming: without these the browser never learns the
  // response is a byte range, and players (notably iOS Safari)
  // refuse to play the film even when the range itself arrives.
  "content-range",
  "accept-ranges",
];

async function proxy(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  if (!BACKEND_URL) {
    return Response.json(
      {
        error: {
          code: "api_proxy_not_configured",
          message:
            "The API proxy is not configured on this deployment (CINEMORA_BACKEND_URL).",
        },
      },
      { status: 503 },
    );
  }

  const { path } = await params;
  const target = `${BACKEND_URL}/api/${path
    .map((segment) => encodeURIComponent(segment))
    .join("/")}${request.nextUrl.search}`;

  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const accept = request.headers.get("accept");
  if (accept) headers.set("accept", accept);
  // The session cookie is the credential (Phase 8.5). Forwarded
  // verbatim; the backend resolves it to the signed-in user.
  const cookie = request.headers.get("cookie");
  if (cookie) headers.set("cookie", cookie);
  // Media streaming: the backend honours Range (206 + Content-Range),
  // but only if the player's Range header actually reaches it. Without
  // this the backend returns the whole file with a 200, which breaks
  // in-browser playback and makes large downloads fail outright.
  const range = request.headers.get("range");
  if (range) headers.set("range", range);
  const ifRange = request.headers.get("if-range");
  if (ifRange) headers.set("if-range", ifRange);
  // Deliberately NOT forwarded: x-api-key (see the header comment).
  // Forwarded-host pinning: the backend's public ingress (GitHub
  // Codespaces port forwarding) refuses requests whose
  // X-Forwarded-Host names a different host — and platform egress
  // stamps this fetch with the SITE's host, which the tunnel then
  // rejects with an empty 404. Pin the forwarded headers to the
  // backend's own origin so the tunnel accepts the request.
  const backendOrigin = new URL(BACKEND_URL);
  headers.set("x-forwarded-host", backendOrigin.host);
  headers.set("x-forwarded-proto", backendOrigin.protocol.replace(":", ""));

  const method = request.method;
  const hasBody = method !== "GET" && method !== "HEAD";
  const body = hasBody ? await request.arrayBuffer() : undefined;

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method,
      headers,
      body,
      redirect: "manual",
    });
  } catch {
    return Response.json(
      {
        error: {
          code: "api_proxy_unreachable",
          message: "The Cinemora backend could not be reached.",
        },
      },
      { status: 502 },
    );
  }

  const responseHeaders = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  // Session lifecycle: login/register set the cookie, logout
  // clears it — Set-Cookie must reach the browser or nobody can
  // ever sign in through this proxy. getSetCookie preserves
  // multiple cookies as separate headers.
  const setCookies =
    typeof upstream.headers.getSetCookie === "function"
      ? upstream.headers.getSetCookie()
      : [];
  if (setCookies.length > 0) {
    for (const value of setCookies) {
      responseHeaders.append("set-cookie", value);
    }
  } else {
    const single = upstream.headers.get("set-cookie");
    if (single) responseHeaders.set("set-cookie", single);
  }
  const location = upstream.headers.get("location");
  if (location) responseHeaders.set("location", location);

  return new Response(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
export const HEAD = proxy;
