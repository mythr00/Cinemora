/**
 * Item 4: the workspace-level Team Space page renders the
 * account's collaboration graph from GET /team/overview —
 * people, projects, invitations — with no video anywhere.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TeamSpacePage from "@/app/team/page";
import { getTeamOverview } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getTeamOverview: vi.fn(),
    getCurrentUser: vi.fn().mockResolvedValue({
      id: "u1",
      email: "owner@example.com",
      display_name: "Owner",
      role: "user",
      status: "active",
    }),
    listProjects: vi.fn().mockResolvedValue({ projects: [] }),
    inviteProjectMember: vi.fn(),
    respondToInvitation: vi.fn(),
  };
});

vi.mock("next/navigation", () => ({
  usePathname: () => "/team",
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

const mockOverview = vi.mocked(getTeamOverview);

beforeEach(() => {
  vi.clearAllMocks();
  mockOverview.mockResolvedValue({
    projects: [
      {
        project_id: "p1",
        project_title: "Harbour Film",
        project_category: "industrial",
        your_role: "owner",
        owner: { user_id: "u1", email: "owner@example.com", display_name: "Owner" },
        members: [
          {
            user: { user_id: "u2", email: "editor@example.com", display_name: null },
            role: "editor",
            status: "accepted",
          },
        ],
        invitations: [],
      },
    ],
    people: [
      {
        user: { user_id: "u2", email: "editor@example.com", display_name: null },
        projects: [{ project_id: "p1", project_title: "Harbour Film", role: "editor" }],
      },
    ],
    invitations_in: [],
  });
});

describe("Team Space page (item 4)", () => {
  it("renders people, projects and the invite form", async () => {
    render(<TeamSpacePage />);
    await waitFor(() =>
      expect(screen.getAllByText("Harbour Film").length).toBeGreaterThan(0),
    );
    expect(screen.getAllByText(/editor@example.com/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Invite a collaborator/)).toBeTruthy();
    expect(
      screen.getByText(/does not need a finished video/),
    ).toBeTruthy();
  });

  it("is reachable from primary navigation", async () => {
    render(<TeamSpacePage />);
    await waitFor(() =>
      expect(screen.getAllByText("Harbour Film").length).toBeGreaterThan(0),
    );
    const teamLinks = screen.getAllByRole("link", { name: /Team/ });
    expect(teamLinks.length).toBeGreaterThan(0);
  });
});
