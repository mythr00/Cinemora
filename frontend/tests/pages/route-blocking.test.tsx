/**
 * Route-level blocking for the FS-gated pages (Completeness
 * S-1 closure, 2026-11-26).
 *
 * The Phase 9 suite pinned the AdminOnlyGate component and the
 * editor-gate predicate, but no test rendered the actual gated
 * ROUTES. These tests render each gated page's default export
 * with lib/api mocked at the module boundary and assert the
 * route-level behaviour an ordinary user meets on a direct URL:
 *
 * - Library, Templates and account Intelligence (FS-1/FS-13)
 *   and the project-internal surfaces — Visual Plan (FS-5),
 *   Research (FS-6), Asset Decisions (FS-7), Graphics (FS-13),
 *   Delivery & Provenance (FS-9), QA Report (FS-10), Versions
 *   (FS-11) and project Intelligence (FS-13) — render the plain
 *   not-found view; their internal UI never mounts.
 * - The Editor and Editor Studio routes (FS-8) render the
 *   honest not-ready state — never the editing UI — while the
 *   project has no completed run with a finished film, in both
 *   the still-processing and the failed framings.
 * - One admin positive control (Graphics route) proves the
 *   same page DOES mount its content for an admin, so the
 *   blocking assertions are not vacuous.
 *
 * The backend enforces the same rules server-side (asserted by
 * backend/tests/test_fs_access_rules.py); these tests pin the
 * frontend route behaviour only. No device/browser click-through
 * is performed here — that residual is recorded in the closure
 * report.
 */
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  getCurrentUser,
  getEditor,
  getOutputs,
  getProjectStatus,
  listProjects,
  listRuns,
} from "@/lib/api";
import { makeOutputs, makeRun, makeStatus, makeUser } from "../helpers/fixtures";

import AssetLibraryPage from "@/app/library/page";
import TemplatesPage from "@/app/templates/page";
import AccountIntelligencePage from "@/app/intelligence/page";
import PlanReviewPage from "@/app/projects/[id]/plan/page";
import ResearchStudioPage from "@/app/projects/[id]/research/page";
import ProjectAssetsPage from "@/app/projects/[id]/assets/page";
import ProjectGraphicsPage from "@/app/projects/[id]/graphics/page";
import CertificatePage from "@/app/projects/[id]/certificate/page";
import QaReportPage from "@/app/projects/[id]/qa/page";
import VersionsPage from "@/app/projects/[id]/versions/page";
import ProjectIntelligencePage from "@/app/projects/[id]/intelligence/page";
import EditorPage from "@/app/projects/[id]/edit/page";
import StudioPage from "@/app/projects/[id]/studio/page";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getCurrentUser: vi.fn(),
    getProjectStatus: vi.fn(),
    getOutputs: vi.fn(),
    listRuns: vi.fn(),
    listProjects: vi.fn(),
    getEditor: vi.fn(),
  };
});

const mockUser = vi.mocked(getCurrentUser);
const mockStatus = vi.mocked(getProjectStatus);
const mockOutputs = vi.mocked(getOutputs);
const mockRuns = vi.mocked(listRuns);
const mockProjects = vi.mocked(listProjects);
const mockEditor = vi.mocked(getEditor);

const NOT_FOUND = "This page could not be found.";

beforeEach(() => {
  vi.clearAllMocks();
  mockUser.mockResolvedValue(makeUser("user"));
  mockProjects.mockResolvedValue({ projects: [] });
  mockStatus.mockResolvedValue(
    makeStatus({ title: "Test project", status: "processing" }),
  );
  mockOutputs.mockResolvedValue(makeOutputs(false));
  mockRuns.mockResolvedValue({ runs: [] });
  mockEditor.mockRejectedValue(new Error("not loaded in this test"));
});

/** The gated internal pages: [name, component, internal heading]. */
const GATED_PAGES: Array<[string, () => React.JSX.Element, string]> = [
  ["Library (/library)", AssetLibraryPage, "Asset Library"],
  ["Templates (/templates)", TemplatesPage, "Templates"],
  ["Account Intelligence (/intelligence)", AccountIntelligencePage, "Intelligence"],
  ["Visual Plan (/projects/[id]/plan)", PlanReviewPage, "Production plan"],
  ["Research (/projects/[id]/research)", ResearchStudioPage, "Research"],
  ["Asset Decisions (/projects/[id]/assets)", ProjectAssetsPage, "Asset decisions"],
  ["Graphics (/projects/[id]/graphics)", ProjectGraphicsPage, "Motion graphics"],
  ["Delivery & Provenance (/projects/[id]/certificate)", CertificatePage, "Delivery & provenance"],
  ["QA Report (/projects/[id]/qa)", QaReportPage, "QA report"],
  ["Versions (/projects/[id]/versions)", VersionsPage, "Versions"],
  ["Project Intelligence (/projects/[id]/intelligence)", ProjectIntelligencePage, "Intelligence"],
];

describe("gated internal routes — ordinary user on a direct URL", () => {
  it.each(GATED_PAGES)(
    "%s renders the not-found view and never mounts its internal UI",
    async (_name, Page, internalHeading) => {
      render(<Page />);
      expect(await screen.findByText(NOT_FOUND)).toBeInTheDocument();
      expect(screen.getByText("404")).toBeInTheDocument();
      expect(screen.queryByText(internalHeading)).toBeNull();
    },
  );

  it.each(GATED_PAGES)(
    "%s blocks a signed-out visitor the same way",
    async (_name, Page, internalHeading) => {
      mockUser.mockResolvedValue(null);
      render(<Page />);
      expect(await screen.findByText(NOT_FOUND)).toBeInTheDocument();
      expect(screen.queryByText(internalHeading)).toBeNull();
    },
  );
});

describe("gated internal routes — admin positive control", () => {
  it("the Graphics route mounts its content for an admin", async () => {
    mockUser.mockResolvedValue(makeUser("admin"));
    mockEditor.mockResolvedValue({
      document: { revision_number: 1, tracks: { visual: [] } },
    } as never);
    render(<ProjectGraphicsPage />);
    expect(
      await screen.findByRole("heading", { name: "Motion graphics" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(NOT_FOUND)).toBeNull();
  });
});

describe("Studio routes — gated on successful completion (FS-8)", () => {
  it("the Editor route shows the not-ready state while generation is incomplete", async () => {
    render(<EditorPage />);
    expect(
      await screen.findByText("Your video isn't ready yet"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Editing opens when generation completes successfully/),
    ).toBeInTheDocument();
    // The editing UI never mounts: no editor document was loaded.
    expect(mockEditor).not.toHaveBeenCalled();
  });

  it("the Editor Studio route shows the not-ready state while generation is incomplete", async () => {
    render(<StudioPage />);
    expect(
      await screen.findByText("Your video isn't ready yet"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Editing opens when generation completes successfully/),
    ).toBeInTheDocument();
    expect(mockEditor).not.toHaveBeenCalled();
  });

  it("the Editor route uses the failed framing when the latest run failed with no completed run behind it", async () => {
    mockStatus.mockResolvedValue(
      makeStatus({ title: "Test project", status: "failed" }),
    );
    mockRuns.mockResolvedValue({ runs: [makeRun("failed")] });
    render(<EditorPage />);
    expect(
      await screen.findByText("Your video isn't ready yet"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Video generation failed, so there is no finished film to edit/),
    ).toBeInTheDocument();
    expect(mockEditor).not.toHaveBeenCalled();
  });

  it("the Editor Studio route uses the failed framing when the latest run failed", async () => {
    mockStatus.mockResolvedValue(
      makeStatus({ title: "Test project", status: "failed" }),
    );
    mockRuns.mockResolvedValue({ runs: [makeRun("failed")] });
    render(<StudioPage />);
    expect(
      await screen.findByText("Your video isn't ready yet"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Video generation failed, so there is no finished film to edit/),
    ).toBeInTheDocument();
    expect(mockEditor).not.toHaveBeenCalled();
  });
});
