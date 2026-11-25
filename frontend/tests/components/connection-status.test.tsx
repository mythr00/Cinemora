/**
 * ApiConnectionBadge (C-06 / A-02): renders the classified
 * connection state as icon + short label + tone. A 401
 * (signed-out) is NEUTRAL, never error-styled; configuration
 * problems are warnings with their own wording; an unreachable
 * backend is the bad state. The classifier itself is covered
 * against real error codes in tests/lib/api-connection.test.ts;
 * here checkApiConnection is mocked to feed each state.
 */
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiConnectionBadge } from "@/components/connection-status";
import { checkApiConnection, type ApiConnectionStatus } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, checkApiConnection: vi.fn() };
});

const mockCheck = vi.mocked(checkApiConnection);

function badgeElement() {
  return screen.getByRole("status");
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ApiConnectionBadge", () => {
  it("shows a checking state before the first probe resolves", () => {
    mockCheck.mockReturnValue(new Promise(() => undefined));
    render(<ApiConnectionBadge />);
    expect(screen.getByText(/Checking API connection/)).toBeInTheDocument();
  });

  it("renders the connected state with the ok tone", async () => {
    mockCheck.mockResolvedValue({
      state: "connected",
      message: "Authenticated API connection confirmed.",
    } satisfies ApiConnectionStatus);
    render(<ApiConnectionBadge />);
    expect(await screen.findByText("API connected")).toBeInTheDocument();
    expect(badgeElement()).toHaveClass("connection-ok");
  });

  it("renders a 401 signed-out state as NEUTRAL, not an error", async () => {
    mockCheck.mockResolvedValue({
      state: "signed-out",
      message: "You are signed out. Sign in to continue.",
    } satisfies ApiConnectionStatus);
    render(<ApiConnectionBadge />);
    expect(await screen.findByText("Signed out")).toBeInTheDocument();
    expect(badgeElement()).toHaveClass("connection-neutral");
    expect(badgeElement()).not.toHaveClass("connection-bad");
    // The detailed message is exposed to assistive technology.
    expect(
      screen.getByText("You are signed out. Sign in to continue."),
    ).toBeInTheDocument();
  });

  it("renders the proxy-not-configured wording for 503 api_proxy_not_configured", async () => {
    mockCheck.mockResolvedValue({
      state: "proxy-not-configured",
      message:
        "The API proxy is not configured on this deployment (CINEMORA_BACKEND_URL is not set), so requests never reach the backend.",
    } satisfies ApiConnectionStatus);
    render(<ApiConnectionBadge />);
    expect(
      await screen.findByText("API proxy not configured"),
    ).toBeInTheDocument();
    expect(badgeElement()).toHaveClass("connection-warn");
  });

  it("renders the server-not-configured wording for 503 api_not_configured", async () => {
    mockCheck.mockResolvedValue({
      state: "backend-not-configured",
      message:
        "The studio API is running but not configured: CINEMORA_API_TOKEN is not set on the server, so it refuses credential-less calls.",
    } satisfies ApiConnectionStatus);
    render(<ApiConnectionBadge />);
    expect(
      await screen.findByText("API not configured on server"),
    ).toBeInTheDocument();
    expect(badgeElement()).toHaveClass("connection-warn");
  });

  it("renders backend unreachable for the proxy 502 state", async () => {
    mockCheck.mockResolvedValue({
      state: "network",
      message:
        "The studio API could not be reached: the backend is unreachable. Check that it is running, then try again.",
    } satisfies ApiConnectionStatus);
    render(<ApiConnectionBadge />);
    expect(
      await screen.findByText("Studio API unreachable"),
    ).toBeInTheDocument();
    expect(badgeElement()).toHaveClass("connection-bad");
  });

  it("renders generic server errors as unavailable", async () => {
    mockCheck.mockResolvedValue({
      state: "unavailable",
      message: "The studio API answered with an error (500). Something broke",
    } satisfies ApiConnectionStatus);
    render(<ApiConnectionBadge />);
    expect(
      await screen.findByText("Studio API unavailable"),
    ).toBeInTheDocument();
    expect(badgeElement()).toHaveClass("connection-bad");
  });

  it("reports the resolved status to the onStatus callback", async () => {
    const status: ApiConnectionStatus = {
      state: "connected",
      message: "Authenticated API connection confirmed.",
    };
    mockCheck.mockResolvedValue(status);
    const onStatus = vi.fn();
    render(<ApiConnectionBadge onStatus={onStatus} />);
    expect(await screen.findByText("API connected")).toBeInTheDocument();
    expect(onStatus).toHaveBeenCalledWith(status);
  });
});
