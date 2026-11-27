/**
 * Music library picker (CINEMATIC MUSIC LIBRARY mandate,
 * 2026-11-27): the production configuration form's Studio
 * library source must let the user browse the real catalogue,
 * search it, filter by category, preview a track (play/pause)
 * and select one — with the selection landing in the config
 * document's music.track_id, which is what the backend
 * resolves at generation time.
 *
 * lib/api is mocked at the module boundary; the preview fetch
 * is the only network the picker performs.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProductionConfigForm } from "@/components/production-config-form";
import { fetchMusicPreviewObjectUrl } from "@/lib/api";
import type { ConfigCatalogue, ProductionConfig } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchMusicPreviewObjectUrl: vi.fn(),
  };
});

const mockPreview = vi.mocked(fetchMusicPreviewObjectUrl);

const TRACKS: ConfigCatalogue["music_library"]["tracks"] = [
  {
    track_id: "epic-battle-speech-wayne-jones",
    title: "Epic Battle Speech",
    composer: null,
    performer: "Wayne Jones",
    moods: ["epic", "battle", "speech"],
    genres: ["epic_battles"],
    category: "epic_battles",
    duration_seconds: 111.5,
    licence_state: "unknown",
    source_class: "bundled",
    has_preview: true,
  },
  {
    track_id: "war-drums",
    title: "War Drums",
    composer: null,
    performer: null,
    moods: ["drums", "war"],
    genres: ["military_drums"],
    category: "military_drums",
    duration_seconds: 95.0,
    licence_state: "unknown",
    source_class: "bundled",
    has_preview: true,
  },
];

function makeCatalogue(): ConfigCatalogue {
  return {
    themes: [],
    custom_theme: { bases: [], token_keys: [] },
    presets: [],
    caption_modes: ["burned"],
    caption_syncs: ["narration"],
    music_placements: ["throughout"],
    music_library: { configured: true, available: true, tracks: TRACKS },
    narration: {
      adapter: "kokoro",
      provider: "kokoro",
      voices: [],
      note: "Choose a voice.",
    },
  };
}

function makeConfig(): ProductionConfig {
  return {
    version: 1,
    preset: "custom",
    motion_graphics: { enabled: true, intensity: "auto" },
    theme: { id: null },
    music: {
      enabled: true,
      source: "library",
      upload: null,
      track_id: null,
      mood: null,
      volume: null,
      placement: "throughout",
      sections: [],
      mix: {},
    },
    narration: { source: "library", voice_id: null, voice_recording: null },
    captions: { mode: "burned", sync: "narration" },
  };
}

describe("music library picker", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPreview.mockResolvedValue({
      url: "blob:preview",
      contentType: "audio/mpeg",
    });
    // jsdom has no media playback; the picker only needs the
    // calls to exist and not throw.
    window.HTMLMediaElement.prototype.play = vi
      .fn()
      .mockResolvedValue(undefined);
    window.HTMLMediaElement.prototype.pause = vi.fn();
    URL.createObjectURL = vi.fn(() => "blob:preview");
    URL.revokeObjectURL = vi.fn();
  });

  it("browses the catalogue with artist, category and duration", () => {
    render(
      <ProductionConfigForm
        catalogue={makeCatalogue()}
        value={makeConfig()}
        onChange={vi.fn()}
        mode="create"
      />,
    );
    expect(screen.getByText("Epic Battle Speech")).toBeInTheDocument();
    expect(screen.getByText(/Wayne Jones/)).toBeInTheDocument();
    // "Epic Battles" appears in the track meta AND the category
    // filter's options — both are the category surfacing.
    expect(screen.getAllByText(/Epic Battles/).length).toBeGreaterThan(0);
    expect(screen.getByText(/1:52/)).toBeInTheDocument();
    expect(screen.getByText("War Drums")).toBeInTheDocument();
    expect(screen.getAllByText(/Military Drums/).length).toBeGreaterThan(0);
  });

  it("search and category filter narrow the list", () => {
    render(
      <ProductionConfigForm
        catalogue={makeCatalogue()}
        value={makeConfig()}
        onChange={vi.fn()}
        mode="create"
      />,
    );
    fireEvent.change(screen.getByLabelText("Search music library"), {
      target: { value: "wayne" },
    });
    expect(screen.getByText("Epic Battle Speech")).toBeInTheDocument();
    expect(screen.queryByText("War Drums")).toBeNull();

    fireEvent.change(screen.getByLabelText("Search music library"), {
      target: { value: "" },
    });
    fireEvent.change(screen.getByLabelText("Filter by category"), {
      target: { value: "military_drums" },
    });
    expect(screen.queryByText("Epic Battle Speech")).toBeNull();
    expect(screen.getByText("War Drums")).toBeInTheDocument();
  });

  it("selecting a track writes its track_id into the config", () => {
    const onChange = vi.fn();
    render(
      <ProductionConfigForm
        catalogue={makeCatalogue()}
        value={makeConfig()}
        onChange={onChange}
        mode="create"
      />,
    );
    fireEvent.click(screen.getByText("War Drums"));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        music: expect.objectContaining({ track_id: "war-drums" }),
      }),
    );
  });

  it("preview button fetches the track preview and plays it", async () => {
    render(
      <ProductionConfigForm
        catalogue={makeCatalogue()}
        value={makeConfig()}
        onChange={vi.fn()}
        mode="create"
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Preview War Drums" }),
    );
    await waitFor(() =>
      expect(mockPreview).toHaveBeenCalledWith("war-drums"),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Stop preview of War Drums" }),
      ).toBeInTheDocument(),
    );
  });

  it("a failed preview is reported, never faked as playing", async () => {
    mockPreview.mockRejectedValue(new Error("no preview"));
    render(
      <ProductionConfigForm
        catalogue={makeCatalogue()}
        value={makeConfig()}
        onChange={vi.fn()}
        mode="create"
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Preview War Drums" }),
    );
    expect(
      await screen.findByText(/preview could not be loaded/),
    ).toBeInTheDocument();
  });
});
