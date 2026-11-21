/**
 * Server-side API proxy (F5 deployment).
 *
 * In deployed builds the browser client runs in "same-origin" mode
 * (NEXT_PUBLIC_API_BASE_URL=same-origin): every API call goes to
 * this origin's /api/* routes, i.e. here. This handler forwards the
 * request to the real backend and injects the real API token from
 * the server-only CINEMORA_API_TOKEN environment variable,
 * replacing whatever the browser sent. The real token therefore
 * never appears in the public JavaScript bundle — the bundle only
 * carries a non-empty placeholder so the client's connection check
 * can run (see lib/api.ts).
 *
 * Server-only configuration (never NEXT_PUBLIC_*):
 * - CINEMORA_BACKEND_URL: origin of the Cinemora backend.
 * - CINEMORA_API_TOKEN: the token matching the backend's own
 *   CINEMORA_API_TOKEN.
 */

import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const BACKEND_URL = (process.env.CINEMORA_BACKEND_URL || "").replace(
  /\/$/,
  "",
);
const API_TOKEN = process.env.CINEMORA_API_TOKEN || "";

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
  if (!BACKEND_URL || !API_TOKEN) {
    return Response.json(
      {
        error: {
          code: "api_proxy_not_configured",
          message:
            "The API proxy is not configured on this deployment (CINEMORA_BACKEND_URL / CINEMORA_API_TOKEN).",
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
  // Media streaming: the backend honours Range (206 + Content-Range),
  // but only if the player's Range header actually reaches it. Without
  // this the backend returns the whole file with a 200, which breaks
  // in-browser playback and makes large downloads fail outright.
  const range = request.headers.get("range");
  if (range) headers.set("range", range);
  const ifRange = request.headers.get("if-range");
  if (ifRange) headers.set("if-range", ifRange);
  headers.set("x-api-key", API_TOKEN);

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
