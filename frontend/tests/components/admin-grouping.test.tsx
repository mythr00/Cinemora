/**
 * Admin workspace grouping (A-12 closure, 2026-11-26,
 * completeness S-4).
 *
 * A-12's criterion was "workspace tabs grouped; navigation
 * verified in UI review". The user workspace was solved in FS
 * by removal (15 tabs → 6); the ADMIN workspace still showed
 * one flat, ungrouped set. These tests pin the grouped
 * presentation in both admin navigation surfaces:
 *
 * - ProjectTabs: an admin sees all fourteen destinations under
 *   three labelled groups (Workspace / Production internals /
 *   Delivery & records), every tab still a link to its original
 *   href; an ordinary user sees exactly the six Workspace tabs,
 *   no group labels and no admin-only tabs — the pre-grouping
 *   bar, unchanged — with the Editor tabs inert while the
 *   completion gate has not passed.
 * - AdminShell: the twelve admin destinations render under
 *   Operations / Accounts & billing / System; an ordinary user
 *   gets the not-found view and none of the navigation.
 *
 * Presentation only: role filtering and the editor gate are
 * exercised through the same lib/api mocks as the rest of the
 * suite. No device/browser click-through is performed here —
 * that residual is recorded in the closure report.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AdminShell } from "@/components/admin-shell";
import { ProjectTabs } from "@/components/project-tabs";
import {
  getCurrentUser,
  getOutputs,
  getProjectStatus,
  listRuns,
} from "@/lib/api";
import { makeOutputs, makeRun, makeStatus, makeUser } from "../helpers/fixtures";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getCurrentUser: vi.fn(),
    getProjectStatus: vi.fn(),
    getOutputs: vi.fn(),
    listRuns: vi.fn(),
  };
});

const mockUser = vi.mocked(getCurrentUser);
const mockStatus = vi.mocked(getProjectStatus);
const mockOutputs = vi.mocked(getOutputs);
const mockRuns = vi.mocked(listRuns);

const USER_TABS = [
  "Overview",
  "Configuration",
  "Editor",
  "Editor Studio",
  "Thumbnails",
  "Localization",
];
const ADMIN_TABS = [
  "Visual Plan",
  "Research",
  "Assets",
  "Graphics",
  "Intelligence",
  "Delivery",
  "QA Report",
  "Versions",
];

beforeEach(() => {
  vi.clearAllMocks();
  mockStatus.mockResolvedValue(
    makeStatus({ title: "Test project", status: "processing" }),
  );
  mockOutputs.mockResolvedValue(makeOutputs(false));
  mockRuns.mockResolvedValue({ runs: [] });
});

describe("ProjectTabs grouping", () => {
  it("groups all fourteen destinations under three labels for an admin", async () => {
    mockUser.mockResolvedValue(makeUser("admin"));
    mockStatus.mockResolvedValue(
      makeStatus({ title: "Test project", status: "completed" }),
    );
    mockOutputs.mockResolvedValue(makeOutputs(true));
    mockRuns.mockResolvedValue({ runs: [makeRun("completed")] });
    render(<ProjectTabs projectId="project-1" />);
    const nav = await screen.findByRole("navigation", {
      name: "Project workspace",
    });
    for (const group of ["Workspace", "Production internals", "Delivery & records"]) {
      expect(within(nav).getByText(group)).toBeInTheDocument();
    }
    for (const label of [...USER_TABS, ...ADMIN_TABS]) {
      expect(
        within(nav).getByRole("link", { name: label }),
      ).toBeInTheDocument();
    }
    // Grouping changed no destination: hrefs are the originals.
    expect(
      within(nav).getByRole("link", { name: "QA Report" }),
    ).toHaveAttribute("href", "/projects/project-1/qa");
    expect(
      within(nav).getByRole("link", { name: "Editor Studio" }),
    ).toHaveAttribute("href", "/projects/project-1/studio");
  });

  it("shows an ordinary user the single unlabelled Workspace group, unchanged", async () => {
    mockUser.mockResolvedValue(makeUser("user"));
    render(<ProjectTabs projectId="project-1" />);
    const nav = await screen.findByRole("navigation", {
      name: "Project workspace",
    });
    for (const label of USER_TABS) {
      expect(within(nav).getByText(label)).toBeInTheDocument();
    }
    for (const label of ADMIN_TABS) {
      expect(within(nav).queryByText(label)).toBeNull();
    }
    for (const group of ["Workspace", "Production internals", "Delivery & records"]) {
      expect(within(nav).queryByText(group)).toBeNull();
    }
  });

  it("keeps the Editor tabs inert for an ordinary user until the gate passes", async () => {
    mockUser.mockResolvedValue(makeUser("user"));
    render(<ProjectTabs projectId="project-1" />);
    // While the gate loads, the tabs render as links (the bar's
    // documented loading behaviour); wait for the resolved
    // not-ready state to replace them with inert spans.
    // (Re-query inside the wait: the state change replaces the
    // link element with a span, so a captured node goes stale.)
    await waitFor(() => {
      expect(screen.getByText("Editor").closest("a")).toBeNull();
    });
    expect(screen.getByText("Editor")).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.getByText("Editor Studio").closest("a")).toBeNull();
  });
});

describe("AdminShell navigation grouping", () => {
  it("renders the thirteen destinations under three group labels for an admin", async () => {
    mockUser.mockResolvedValue(makeUser("super_admin"));
    render(
      <AdminShell>
        <p>Admin home</p>
      </AdminShell>,
    );
    const nav = await screen.findByRole("navigation", {
      name: "Administration",
    });
    for (const group of ["Operations", "Accounts & billing", "System"]) {
      expect(within(nav).getByText(group)).toBeInTheDocument();
    }
    const links = within(nav).getAllByRole("link");
    expect(links).toHaveLength(13);
    expect(
      within(nav).getByRole("link", { name: "Subscriptions overview" }),
    ).toHaveAttribute("href", "/admin/subscriptions");
    expect(
      within(nav).getByRole("link", { name: "Affiliates" }),
    ).toHaveAttribute("href", "/admin/affiliates");
    expect(screen.getByText("Admin home")).toBeInTheDocument();
  });

  it("shows an ordinary user the not-found view and no admin navigation", async () => {
    mockUser.mockResolvedValue(makeUser("user"));
    render(
      <AdminShell>
        <p>Admin home</p>
      </AdminShell>,
    );
    expect(
      await screen.findByText("This page could not be found."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Administration" })).toBeNull();
    expect(screen.queryByText("Admin home")).toBeNull();
  });
});
