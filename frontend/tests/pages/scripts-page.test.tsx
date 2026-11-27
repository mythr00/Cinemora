/**
 * Redesign programme (2026-11-27): the Channel Research flow in
 * Script Studio, end to end at the UI level — read a channel,
 * select at most 3 videos (the 4th is refused WITH an
 * explanation), analyse (basis shown by video title, ideas show
 * why they fit), use an idea (the research reference chip appears
 * on the brief), generate (research_id is sent exactly once and
 * then cleared so it cannot leak into the next brief).
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ScriptsPage from "@/app/scripts/page";
import {
  analyseChannelResearch,
  generateScriptDraft,
  getScriptDraft,
  resolveChannelResearch,
  type ChannelResearchRecord,
  type ScriptDraft,
} from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getCurrentUser: vi.fn().mockResolvedValue({
      id: "u1",
      email: "owner@example.com",
      display_name: "Owner",
      role: "user",
      status: "active",
    }),
    listScriptDrafts: vi.fn().mockResolvedValue({ scripts: [] }),
    listProjects: vi.fn().mockResolvedValue({ projects: [] }),
    getLanguageCatalogue: vi.fn().mockResolvedValue({ languages: [] }),
    listChannelResearch: vi.fn().mockResolvedValue({ records: [] }),
    listScriptRevisions: vi.fn().mockResolvedValue({ revisions: [] }),
    resolveChannelResearch: vi.fn(),
    analyseChannelResearch: vi.fn(),
    generateScriptDraft: vi.fn(),
    getScriptDraft: vi.fn(),
  };
});

vi.mock("next/navigation", () => ({
  usePathname: () => "/scripts",
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

const mockResolve = vi.mocked(resolveChannelResearch);
const mockAnalyse = vi.mocked(analyseChannelResearch);
const mockGenerate = vi.mocked(generateScriptDraft);
const mockGetDraft = vi.mocked(getScriptDraft);

const VIDEOS = [1, 2, 3, 4].map((n) => ({
  id: `v${n}`,
  title: `Video ${["One", "Two", "Three", "Four"][n - 1]}`,
  url: `https://www.youtube.com/watch?v=v${n}`,
  view_count: 1000 * n,
  like_count: null,
  duration_seconds: 600,
  upload_date: "20250101",
  thumbnail: null,
}));

const RECORD: ChannelResearchRecord = {
  id: "research-1",
  channel: {
    id: "UC1",
    title: "Harbour Docs",
    url: "https://www.youtube.com/@HarbourDocs",
    subscriber_count: 42000,
  },
  videos: VIDEOS,
  ordering_basis: "view_count_desc",
  analysis: null,
  created_at: "2026-11-27T00:00:00Z",
};

const ANALYSED: ChannelResearchRecord = {
  ...RECORD,
  analysis: {
    at: "2026-11-27T00:05:00Z",
    basis: { v1: "transcript", v2: "metadata", v3: "metadata" },
    videos: [
      { id: "v1", title: "Video One", basis: "transcript", view_count: 1000, duration_seconds: 600 },
      { id: "v2", title: "Video Two", basis: "metadata", view_count: 2000, duration_seconds: 600 },
      { id: "v3", title: "Video Three", basis: "metadata", view_count: 3000, duration_seconds: 600 },
    ],
    patterns: [
      { pattern: "Numbers in titles", basis: "metadata", evidence: "2 of 3 titles" },
    ],
    ideas: [
      {
        id: "idea-0",
        title: "The Last Lighthouse Keeper",
        angle: "Automation ends a craft",
        why_it_fits: "Single-subject craft story",
      },
    ],
  },
};

const DRAFT: ScriptDraft = {
  id: "d1",
  title: "The Last Lighthouse Keeper",
  video_title: "The Last Lighthouse Keeper",
  language: "en",
  category: "history",
  target_minutes: 10,
  project_id: null,
  productions: [],
  created_at: "2026-11-27T00:10:00Z",
  updated_at: "2026-11-27T00:10:00Z",
  metrics: { word_count: 10, estimated_minutes: 1, sentence_count: 1 },
  section_count: 1,
  has_analysis: false,
  has_fact_check: false,
  sections: [{ id: "s1", heading: "Opening", body: "Narration text." }],
  generation: {
    topic: "The Last Lighthouse Keeper",
    angle: "Automation ends a craft",
    research_id: "research-1",
    research_channel: "Harbour Docs",
    outline: [{ heading: "Opening", intent: "Open.", word_budget: 700 }],
    basis: "llm_generated_from_user_brief",
    at: "2026-11-27T00:10:00Z",
  },
};

describe("Scripts page — channel research flow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockResolve.mockResolvedValue(RECORD);
    mockAnalyse.mockResolvedValue(ANALYSED);
    mockGenerate.mockResolvedValue(DRAFT);
    mockGetDraft.mockResolvedValue(DRAFT);
  });

  it("runs read → select (max 3 explained) → analyse → idea → generate with the reference sent once", async () => {
    render(<ScriptsPage />);

    const urlInput = await screen.findByPlaceholderText(
      "https://www.youtube.com/@SomeChannel",
    );
    fireEvent.change(urlInput, {
      target: { value: "https://www.youtube.com/@HarbourDocs/videos" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Read channel" }));

    // Videos listed after the read.
    const boxes = await screen.findAllByRole("checkbox");
    expect(boxes).toHaveLength(4);
    expect(mockResolve).toHaveBeenCalledWith(
      "https://www.youtube.com/@HarbourDocs/videos",
    );

    // Select three; the fourth is refused and the page says why.
    fireEvent.click(boxes[0]);
    fireEvent.click(boxes[1]);
    fireEvent.click(boxes[2]);
    expect(boxes[3]).toBeDisabled();
    expect(
      screen.getByText(/3 selected — the maximum/),
    ).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: /Analyse 3 selected/ }),
    );

    // Basis is rendered by video TITLE, never raw ids; the idea
    // shows why it fits.
    expect(await screen.findByText(/Video One: transcript/)).toBeInTheDocument();
    expect(screen.queryByText(/v1: transcript/)).not.toBeInTheDocument();
    expect(screen.getByText("Numbers in titles")).toBeInTheDocument();
    expect(
      screen.getByText(/Why it fits: Single-subject craft story/),
    ).toBeInTheDocument();
    expect(mockAnalyse).toHaveBeenCalledWith("research-1", ["v1", "v2", "v3"]);

    // Use the idea: the brief fills and the reference chip shows.
    fireEvent.click(screen.getByRole("button", { name: "Use this idea" }));
    const topic = screen.getByPlaceholderText(
      "The 1981 Hyatt Regency walkway collapse",
    ) as HTMLInputElement;
    expect(topic.value).toBe("The Last Lighthouse Keeper");
    expect(screen.getByText(/Research reference:/)).toBeInTheDocument();
    // "Harbour Docs" appears in the chip and the research panel.
    expect(screen.getAllByText("Harbour Docs").length).toBeGreaterThan(1);

    // Generate: the research id is sent with the draft…
    fireEvent.click(screen.getByRole("button", { name: "Generate draft" }));
    await waitFor(() => expect(mockGenerate).toHaveBeenCalledTimes(1));
    expect(mockGenerate).toHaveBeenCalledWith(
      expect.objectContaining({ research_id: "research-1" }),
    );

    // …and cleared afterwards, so the next brief starts clean.
    await waitFor(() =>
      expect(screen.queryByText(/Research reference:/)).not.toBeInTheDocument(),
    );
  });

  it("removing the research reference means generate sends none", async () => {
    render(<ScriptsPage />);
    const urlInput = await screen.findByPlaceholderText(
      "https://www.youtube.com/@SomeChannel",
    );
    fireEvent.change(urlInput, {
      target: { value: "https://www.youtube.com/@HarbourDocs" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Read channel" }));
    const boxes = await screen.findAllByRole("checkbox");
    fireEvent.click(boxes[0]);
    fireEvent.click(
      screen.getByRole("button", { name: /Analyse 1 selected/ }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Use this idea" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Remove research reference" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Generate draft" }));
    await waitFor(() => expect(mockGenerate).toHaveBeenCalledTimes(1));
    expect(mockGenerate).toHaveBeenCalledWith(
      expect.objectContaining({ research_id: undefined }),
    );
  });
});
