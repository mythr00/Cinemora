/**
 * Item 5: the standalone Thumbnail Studio page — works from a
 * project's uploads/stills with NO completed film (film frames
 * absent, uploads present), and renders measured channel style
 * analysis from real channel data.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ThumbnailStudioPage from "@/app/thumbnails/page";
import {
  analyseChannelThumbnails,
  generateThumbnailVariants,
  getThumbnails,
  listProjects,
  regenerateThumbnail,
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
    listProjects: vi.fn(),
    getThumbnails: vi.fn(),
    composeThumbnail: vi.fn(),
    generateThumbnailVariants: vi.fn(),
    deleteThumbnail: vi.fn(),
    regenerateThumbnail: vi.fn(),
    suggestThumbnailHeadlines: vi.fn(),
    uploadAsset: vi.fn(),
    analyseChannelThumbnails: vi.fn(),
    fetchAuthenticatedMediaObjectUrl: vi
      .fn()
      .mockRejectedValue(new Error("no media in test")),
  };
});

vi.mock("next/navigation", () => ({
  usePathname: () => "/thumbnails",
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

const mockProjects = vi.mocked(listProjects);
const mockView = vi.mocked(getThumbnails);
const mockChannel = vi.mocked(analyseChannelThumbnails);
const mockVariants = vi.mocked(generateThumbnailVariants);
const mockRegenerate = vi.mocked(regenerateThumbnail);

const VIEW = {
  schema: "thumbnail_studio.v1",
  project_id: "p1",
  spec: {
    width: 1280,
    height: 720,
    max_bytes: 2_000_000,
    layouts: ["bottom_band", "top_band"],
    accents: ["amber", "red"],
    max_headline_words: 8,
    max_headline_characters: 60,
  },
  brief: {
    title: "Harbour Film",
    category: "industrial",
    story_type: null,
    case_bible_state: "none",
    primary_subject: null,
    recorded_facts: [],
    headline_suggestions: ["THE NIGHT THE CRANES FELL"],
  },
  bases: {
    film: { state: "not_recorded", timestamps_source: "none", frames: [] },
    stills: [],
    uploads: [
      {
        base_id: "upload:up1",
        kind: "upload" as const,
        label: "Your upload · crane.jpg",
        upload_id: "up1",
        file_name: "crane.jpg",
      },
    ],
  },
  variants: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  mockProjects.mockResolvedValue({
    projects: [
      {
        id: "p1",
        title: "Harbour Film",
        category: "industrial",
        status: "draft",
        created_at: "2026-11-01T00:00:00Z",
        updated_at: "2026-11-01T00:00:00Z",
      },
    ],
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  mockView.mockResolvedValue(VIEW as any);
});

describe("Thumbnail Studio page (item 5)", () => {
  it("works from uploads with no completed film", async () => {
    render(<ThumbnailStudioPage />);
    expect(await screen.findByText("Thumbnail Studio")).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByText(/Bases available: 0 film frame\(s\) · 0 verified still\(s\) · 1 upload\(s\)/),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/Film frames appear once a production completes/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Your upload · crane\.jpg/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Compose thumbnail" }),
    ).toBeInTheDocument();
  });

  it("renders measured channel style analysis", async () => {
    mockChannel.mockResolvedValue({
      channel: { title: "Harbour Docs", subscriber_count: 42000 },
      ordering_basis: "view_count_desc",
      label:
        "Measured properties of this channel's public thumbnails (palette, OCR text, luminance) — style reference only.",
      videos: [
        {
          id: "v1",
          title: "Big Ships",
          view_count: 5000,
          measurement: {
            dimensions: [320, 180],
            ocr: { state: "ok", lines: ["BIG SHIPS"], word_count: 2 },
            palette: [{ hex: "#c82828", share: 0.5 }],
            luminance: { mean: 0.42, spread: 0.1 },
          },
          measurement_error: null,
        },
      ],
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } as any);
    render(<ThumbnailStudioPage />);
    await screen.findByText("Thumbnail Studio");
    fireEvent.change(screen.getByPlaceholderText("https://www.youtube.com/@SomeChannel"), {
      target: { value: "https://www.youtube.com/@HarbourDocs" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Analyse channel" }));
    expect(await screen.findByText("Big Ships")).toBeInTheDocument();
    expect(screen.getByText(/42,000 subscribers/)).toBeInTheDocument();
    expect(screen.getByText(/#c82828/)).toBeInTheDocument();
    expect(screen.getAllByText(/style reference only/).length).toBeGreaterThan(0);
  });

  it("hides the film-frames hint when frames are recorded", async () => {
    mockView.mockResolvedValue({
      ...VIEW,
      bases: {
        ...VIEW.bases,
        film: {
          state: "recorded",
          timestamps_source: "timeline",
          frames: [
            {
              base_id: "frame:run1:60",
              kind: "film_frame",
              label: "Film frame at 60s",
              timestamp_seconds: 60,
            },
          ],
        },
      },
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } as any);
    render(<ThumbnailStudioPage />);
    expect(await screen.findByText("Thumbnail Studio")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText(/1 film frame\(s\)/)).toBeInTheDocument(),
    );
    expect(
      screen.queryByText(/Film frames appear once a production completes/),
    ).not.toBeInTheDocument();
  });

  it("shows variant skip reasons instead of hiding them", async () => {
    mockVariants.mockResolvedValue({
      created: [],
      skipped: [
        {
          base_id: "upload:up1",
          layout: "bottom_band",
          code: "base_unreadable",
          message: "The base image could not be read.",
        },
      ],
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } as any);
    render(<ThumbnailStudioPage />);
    expect(await screen.findByText("Thumbnail Studio")).toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole("button", { name: "Generate variations" }),
    );
    expect(
      await screen.findByText(/The base image could not be read\./),
    ).toBeInTheDocument();
  });

  it("renders a missing-file variant honestly, with regenerate", async () => {
    mockView.mockResolvedValue({
      ...VIEW,
      variants: [
        {
          id: "t1",
          created_at: "2026-11-01T00:00:00Z",
          file_name: "gone.png",
          base: { kind: "upload", upload_id: "up1", file_name: "crane.jpg" },
          base_id: "upload:up1",
          headline: "THE NIGHT THE CRANES FELL",
          layout: "bottom_band",
          accent: "amber",
          checks: {
            dimensions: [1280, 720],
            format: "PNG",
            file_size_bytes: 900000,
            under_youtube_size_limit: true,
            headline_word_count: 5,
            headline_character_count: 24,
            headline_contrast_ratio: 4.5,
            headline_font_height_px_at_10_percent_scale: 9.6,
          },
          available: false,
        },
      ],
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } as any);
    mockRegenerate.mockResolvedValue({} as never);
    render(<ThumbnailStudioPage />);
    expect(await screen.findByText("Thumbnail Studio")).toBeInTheDocument();
    expect(
      await screen.findByText(/no longer on disk/),
    ).toBeInTheDocument();
    // No download is offered for a file that does not exist.
    expect(screen.queryByText("Download")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Regenerate/ }));
    await waitFor(() =>
      expect(mockRegenerate).toHaveBeenCalledWith("p1", "t1"),
    );
  });
});
