/**
 * AdminOnlyGate (FS-B/FS-C): children render ONLY for admin /
 * super_admin principals. Ordinary users and signed-out visitors
 * get the plain not-found view; an unresolved role fails closed
 * (loading, no children); a failed role load shows the error
 * state, never the children. lib/api is mocked at the boundary.
 */
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AdminOnlyGate } from "@/components/admin-only-gate";
import { getCurrentUser } from "@/lib/api";
import { makeUser } from "../helpers/fixtures";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, getCurrentUser: vi.fn() };
});

const mockUser = vi.mocked(getCurrentUser);
const SECRET = "Internal diagnostic surface";

function renderGate() {
  return render(
    <AdminOnlyGate>
      <div>{SECRET}</div>
    </AdminOnlyGate>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("AdminOnlyGate", () => {
  it("renders children for an admin", async () => {
    mockUser.mockResolvedValue(makeUser("admin"));
    renderGate();
    expect(await screen.findByText(SECRET)).toBeInTheDocument();
  });

  it("renders children for a super_admin", async () => {
    mockUser.mockResolvedValue(makeUser("super_admin"));
    renderGate();
    expect(await screen.findByText(SECRET)).toBeInTheDocument();
  });

  it("blocks an ordinary user with the not-found view", async () => {
    mockUser.mockResolvedValue(makeUser("user"));
    renderGate();
    expect(
      await screen.findByText("This page could not be found."),
    ).toBeInTheDocument();
    expect(screen.getByText("404")).toBeInTheDocument();
    expect(screen.queryByText(SECRET)).toBeNull();
  });

  it("blocks a signed-out visitor the same way", async () => {
    mockUser.mockResolvedValue(null);
    renderGate();
    expect(
      await screen.findByText("This page could not be found."),
    ).toBeInTheDocument();
    expect(screen.queryByText(SECRET)).toBeNull();
  });

  it("fails closed while the role is unresolved: loading, no children", () => {
    mockUser.mockReturnValue(new Promise(() => undefined));
    renderGate();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText(SECRET)).toBeNull();
    expect(screen.queryByText("This page could not be found.")).toBeNull();
  });

  it("shows the error state (not the children) when the role load fails", async () => {
    mockUser.mockRejectedValue(new Error("network down"));
    renderGate();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(SECRET)).toBeNull();
  });
});
