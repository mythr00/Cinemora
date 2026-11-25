/**
 * checkApiConnection classification (lib/api, C-06 / A-02): the
 * REAL classifier, driven by stubbing fetch — the same responses
 * the same-origin proxy / backend produce in production. Each
 * error code must map to its own honest diagnosis; in particular
 * only the exact api_not_configured code may carry the
 * operator-token diagnosis, and a 401 is the neutral signed-out
 * state, never an error.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { checkApiConnection } from "@/lib/api";

function stubFetch(response: { ok: boolean; status: number; body: unknown }) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: response.ok,
      status: response.status,
      json: async () => response.body,
    })),
  );
}

function errorResponse(status: number, code?: string, message = "boom") {
  stubFetch({
    ok: false,
    status,
    body: { error: { code, message } },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("checkApiConnection", () => {
  it("reports connected when the authenticated probe succeeds", async () => {
    stubFetch({ ok: true, status: 200, body: { projects: [] } });
    const status = await checkApiConnection();
    expect(status.state).toBe("connected");
  });

  it("classifies a 401 as the neutral signed-out state", async () => {
    errorResponse(401, "unauthorized", "No session");
    const status = await checkApiConnection();
    expect(status.state).toBe("signed-out");
    expect(status.message).toContain("signed out");
  });

  it("classifies 503 api_proxy_not_configured as proxy-not-configured", async () => {
    errorResponse(503, "api_proxy_not_configured");
    const status = await checkApiConnection();
    expect(status.state).toBe("proxy-not-configured");
    expect(status.message).toContain("CINEMORA_BACKEND_URL is not set");
  });

  it("classifies 503 api_not_configured as the operator-token diagnosis", async () => {
    errorResponse(503, "api_not_configured");
    const status = await checkApiConnection();
    expect(status.state).toBe("backend-not-configured");
    expect(status.message).toContain("CINEMORA_API_TOKEN is not set");
  });

  it("does NOT blame the token for a 503 with any other code (A-02)", async () => {
    errorResponse(503, "service_unavailable", "Backend is restarting");
    const status = await checkApiConnection();
    expect(status.state).toBe("unavailable");
    expect(status.message).not.toContain("CINEMORA_API_TOKEN");
  });

  it("classifies the proxy's 502 api_proxy_unreachable as backend unreachable", async () => {
    errorResponse(502, "api_proxy_unreachable");
    const status = await checkApiConnection();
    expect(status.state).toBe("network");
    expect(status.message).toContain("backend is unreachable");
  });

  it("classifies any bare 502 as backend unreachable", async () => {
    errorResponse(502);
    const status = await checkApiConnection();
    expect(status.state).toBe("network");
  });

  it("classifies other server errors as unavailable with the status named", async () => {
    errorResponse(500, "internal", "Something broke");
    const status = await checkApiConnection();
    expect(status.state).toBe("unavailable");
    expect(status.message).toContain("500");
  });

  it("classifies a total network failure as unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const status = await checkApiConnection();
    expect(status.state).toBe("network");
    expect(status.message).toContain("could not be reached");
  });
});
