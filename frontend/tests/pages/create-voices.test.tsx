/**
 * Create page narration voice/language behaviour (Kokoro
 * readiness audit D1–D4):
 * - D1: the Step-2 voice list shows only the selected language's
 *   voices, labelled with natural names ("English (US) — Heart
 *   (female)") — never internal voice ids or engine names
 *   (founder directive 2026-11-26 §G) — and "In use" marks only
 *   the voice that will actually be submitted.
 * - D2: the blocked-language notice is composed from the
 *   catalogue's real coverage — never a hardcoded "English only"
 *   claim.
 * - D3: changing the language re-seeds the voice to that
 *   language's primary, so the default submit path sends a valid
 *   language/voice pair (French → ff_siwis, Spanish → ef_dora)
 *   in both the flat voice_id and the production_config.
 * - D4: the Step-4 picker filters its options by the
 *   configuration's narration language, using the per-voice
 *   language the config catalogue now carries.
 * lib/api is mocked at the module boundary with a small,
 * truthfully-shaped catalogue subset.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CreatePage from "@/app/create/page";
import {
  checkApiConnection,
  createProject,
  getBillingSummary,
  getConfigCatalogue,
  getConfigDefaults,
  getCurrentUser,
  getLanguageCatalogue,
  getVoiceCatalogue,
  listProjects,
  submitRun,
} from "@/lib/api";
import { makeUser } from "../helpers/fixtures";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    checkApiConnection: vi.fn(),
    createProject: vi.fn(),
    getBillingSummary: vi.fn(),
    getConfigCatalogue: vi.fn(),
    getConfigDefaults: vi.fn(),
    getCurrentUser: vi.fn(),
    getLanguageCatalogue: vi.fn(),
    getProjectStatus: vi.fn(),
    getVoiceCatalogue: vi.fn(),
    listProjects: vi.fn(),
    submitRun: vi.fn(),
  };
});

const VOICES = [
  { id: "af_heart", language: "en", region: "English (US)", gender: "female", name: "Heart" },
  { id: "am_michael", language: "en", region: "English (US)", gender: "male", name: "Michael" },
  { id: "bf_emma", language: "en", region: "English (UK)", gender: "female", name: "Emma" },
  { id: "ef_dora", language: "es", region: "Spanish", gender: "female", name: "Dora" },
  { id: "em_alex", language: "es", region: "Spanish", gender: "male", name: "Alex" },
  { id: "ff_siwis", language: "fr", region: "French", gender: "female", name: "Siwis" },
];

const LABELS: Record<string, string> = {
  af_heart: "English (US) — Heart (female)",
  am_michael: "English (US) — Michael (male)",
  bf_emma: "English (UK) — Emma (female)",
  ef_dora: "Spanish — Dora (female)",
  em_alex: "Spanish — Alex (male)",
  ff_siwis: "French — Siwis (female)",
};

function voiceCatalogue() {
  return {
    voices: [
      ...VOICES.map((voice) => ({
        id: voice.id,
        is_default: false,
        alias_of: null,
        model: "kokoro-v1.0.onnx",
        model_file: "kokoro-v1.0.onnx",
        config_file: "voices-v1.0.bin",
        installed: true,
        language: voice.language,
        region: voice.region,
        gender: voice.gender,
        name: voice.name,
      })),
      {
        id: "default",
        is_default: true,
        alias_of: "af_heart",
        model: "kokoro-v1.0.onnx",
        model_file: "kokoro-v1.0.onnx",
        config_file: "voices-v1.0.bin",
        installed: true,
        language: "en",
        region: "English (US)",
        gender: "female",
        name: "Heart",
      },
    ],
    default_voice_id: "default",
  };
}

function languageCatalogue() {
  const entry = (
    code: string,
    name: string,
    voices: { id: string; label: string }[],
  ) => ({
    code,
    name,
    narration_supported: voices.length > 0,
    voices: voices.map((voice) => ({
      id: voice.id,
      language: code,
      label: voice.label,
    })),
    script_supported: true,
    translation_supported: true,
  });
  return {
    languages: [
      entry("en", "English", [
        { id: "af_heart", label: "English (US) — Heart (female)" },
        { id: "am_michael", label: "English (US) — Michael (male)" },
        { id: "bf_emma", label: "English (UK) — Emma (female)" },
        { id: "default", label: "English (US) — Heart (female)" },
      ]),
      entry("es", "Spanish", [
        { id: "ef_dora", label: "Spanish — Dora (female)" },
        { id: "em_alex", label: "Spanish — Alex (male)" },
      ]),
      entry("fr", "French", [{ id: "ff_siwis", label: "French — Siwis (female)" }]),
      entry("de", "German", []),
    ],
    default_language: "en",
  };
}

function configCatalogue() {
  return {
    themes: [],
    custom_theme: { bases: [], token_keys: [] },
    presets: [],
    caption_modes: ["burn", "sidecar", "off"],
    caption_syncs: ["sentence"],
    music_placements: ["throughout"],
    music_library: { configured: false, available: false, tracks: [] },
    narration: {
      adapter: "services.narration",
      provider: "kokoro",
      voices: [
        ...VOICES.map((voice) => ({
          id: voice.id,
          is_default: false,
          installed: true,
          language: voice.language,
          region: voice.region,
          gender: voice.gender,
          name: voice.name,
        })),
        {
          id: "default",
          is_default: true,
          installed: true,
          language: "en",
          region: "English (US)",
          gender: "female",
          name: "Heart",
        },
      ],
      note: "Choose the voice your documentary is narrated in — every voice listed speaks the language shown beside it.",
    },
  };
}

function voiceRow(label: string) {
  const name = screen.getAllByText(label, { selector: "strong" })[0];
  return name.closest(".voice-catalog-row") as HTMLElement;
}

function inUseRow() {
  return screen
    .getByText("In use")
    .closest(".voice-catalog-row") as HTMLElement;
}

async function awaitVoice(label: string) {
  return screen.findAllByText(label, { selector: "strong" });
}

async function selectLanguage(code: string) {
  const select = (await screen.findByLabelText(
    /LANGUAGE/,
  )) as HTMLSelectElement;
  fireEvent.change(select, { target: { value: code } });
}

async function submitWithScript() {
  fireEvent.change(screen.getByLabelText("Documentary script"), {
    target: { value: "A short script about a harbour town." },
  });
  const button = await screen.findByRole("button", {
    name: /Create and start/,
  });
  fireEvent.click(button);
  await waitFor(() => expect(createProject).toHaveBeenCalledTimes(1));
  return vi.mocked(createProject).mock.calls[0][0] as unknown as {
    options: {
      language: string;
      voice_id: string;
      production_config?: {
        narration: { voice_id: string | null; language?: string };
      };
    };
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  vi.mocked(getCurrentUser).mockResolvedValue(makeUser("user"));
  vi.mocked(listProjects).mockResolvedValue({ projects: [] } as never);
  vi.mocked(getBillingSummary).mockRejectedValue(new Error("no billing"));
  vi.mocked(checkApiConnection).mockResolvedValue({
    state: "connected",
    message: "Authenticated API connection confirmed.",
  });
  vi.mocked(getVoiceCatalogue).mockResolvedValue(voiceCatalogue() as never);
  vi.mocked(getLanguageCatalogue).mockResolvedValue(
    languageCatalogue() as never,
  );
  // Default: the configuration catalogue is unreachable, so the
  // page submits the flat options (the config-path test below
  // overrides this).
  vi.mocked(getConfigCatalogue).mockRejectedValue(new Error("no catalogue"));
  vi.mocked(getConfigDefaults).mockRejectedValue(new Error("no defaults"));
  vi.mocked(createProject).mockResolvedValue({ id: "project-1" } as never);
  vi.mocked(submitRun).mockResolvedValue({ job_id: "job-1" } as never);
});

describe("Create page voice list (D1)", () => {
  it("shows only the selected language's voices with natural names and one In use", async () => {
    render(<CreatePage />);
    expect((await awaitVoice(LABELS.af_heart)).length).toBeGreaterThan(0);
    // The full English list arrives with the language catalogue
    // (the primary alone can appear earlier via the fallback rows).
    expect((await awaitVoice(LABELS.am_michael)).length).toBe(1);
    expect((await awaitVoice(LABELS.bf_emma)).length).toBe(1);
    // Other languages' voices are not listed for English.
    expect(screen.queryByText(LABELS.ef_dora)).toBeNull();
    expect(screen.queryByText(LABELS.ff_siwis)).toBeNull();
    // Internal voice ids and engine names are never display copy.
    expect(screen.queryByText("af_heart")).toBeNull();
    expect(screen.queryByText("am_michael")).toBeNull();
    expect(document.body.textContent).not.toContain("Kokoro-82M");
    expect(document.body.textContent).not.toContain("Piper");
    // Exactly one voice is in use: the English primary.
    const inUse = screen.getAllByText("In use");
    expect(inUse).toHaveLength(1);
    expect(inUseRow().textContent).toContain(LABELS.af_heart);
    expect(voiceRow(LABELS.am_michael).textContent).not.toContain("In use");
  });

  it("filters the list when the language changes to French", async () => {
    render(<CreatePage />);
    await awaitVoice(LABELS.af_heart);
    await selectLanguage("fr");
    expect((await awaitVoice(LABELS.ff_siwis)).length).toBe(1);
    expect(screen.queryByText(LABELS.af_heart)).toBeNull();
    expect(screen.queryByText(LABELS.ef_dora)).toBeNull();
    expect(voiceRow(LABELS.ff_siwis).textContent).toContain("In use");
    expect(screen.getAllByText("In use")).toHaveLength(1);
  });
});

describe("Create page voice re-seeding (D3)", () => {
  it("submits ff_siwis for a French production through the default path", async () => {
    render(<CreatePage />);
    await awaitVoice(LABELS.af_heart);
    await selectLanguage("fr");
    await awaitVoice(LABELS.ff_siwis);
    const submitted = await submitWithScript();
    expect(submitted.options.language).toBe("fr");
    expect(submitted.options.voice_id).toBe("ff_siwis");
  });

  it("submits ef_dora for a Spanish production through the default path", async () => {
    render(<CreatePage />);
    await awaitVoice(LABELS.af_heart);
    await selectLanguage("es");
    expect((await awaitVoice(LABELS.ef_dora)).length).toBe(1);
    expect(voiceRow(LABELS.ef_dora).textContent).toContain("In use");
    const submitted = await submitWithScript();
    expect(submitted.options.language).toBe("es");
    expect(submitted.options.voice_id).toBe("ef_dora");
  });

  it("re-seeds the production_config voice as well when the catalogue loads", async () => {
    vi.mocked(getConfigCatalogue).mockResolvedValue(
      configCatalogue() as never,
    );
    vi.mocked(getConfigDefaults).mockResolvedValue({
      defaults: null,
    } as never);
    render(<CreatePage />);
    await awaitVoice(LABELS.af_heart);
    await selectLanguage("fr");
    await awaitVoice(LABELS.ff_siwis);
    const submitted = await submitWithScript();
    expect(submitted.options.production_config).toBeDefined();
    expect(submitted.options.production_config!.narration.voice_id).toBe(
      "ff_siwis",
    );
    expect(submitted.options.production_config!.narration.language).toBe(
      "fr",
    );
    expect(submitted.options.voice_id).toBe("ff_siwis");
  });
});

describe("Create page blocked-language notice (D2)", () => {
  it("derives the coverage from the catalogue instead of claiming English only", async () => {
    render(<CreatePage />);
    await awaitVoice(LABELS.af_heart);
    await selectLanguage("de");
    const notice = await screen.findByText(
      /no installed voice speaks German/,
    );
    expect(notice.textContent).toContain("Spanish");
    expect(notice.textContent).toContain("French");
    expect(notice.textContent).not.toContain("English (US) only");
    expect(document.body.textContent).not.toContain(
      "installed voices are English (US) only",
    );
    const button = screen.getByRole("button", { name: /Create and start/ });
    expect(button).toBeDisabled();
  });
});

describe("Configuration voice picker (D4)", () => {
  it("offers only the narration language's voices once a language is chosen", async () => {
    vi.mocked(getConfigCatalogue).mockResolvedValue(
      configCatalogue() as never,
    );
    vi.mocked(getConfigDefaults).mockResolvedValue({
      defaults: null,
    } as never);
    render(<CreatePage />);
    await awaitVoice(LABELS.af_heart);
    await selectLanguage("fr");
    await awaitVoice(LABELS.ff_siwis);
    const picker = (await screen.findByLabelText(
      /VOICE/,
    )) as HTMLSelectElement;
    await waitFor(() => {
      const values = Array.from(picker.options).map((option) => option.value);
      expect(values).toEqual(["", "ff_siwis"]);
    });
    expect(picker.value).toBe("ff_siwis");
  });
});
