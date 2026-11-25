/**
 * Create page draft persistence (Phase 6, A-05) at the page
 * level: a draft saved in localStorage is restored into the
 * form on load with the "Draft restored" indicator; Discard
 * clears the stored draft and empties the form; typing persists
 * a fresh draft (debounced); with no stored draft there is no
 * indicator and the form starts empty. lib/api is mocked at the
 * module boundary (the config catalogue is made unreachable so
 * the page takes its labelled standard-settings fallback and the
 * draft behaviour is what is exercised).
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CreatePage from "@/app/create/page";
import {
  checkApiConnection,
  getBillingSummary,
  getConfigCatalogue,
  getConfigDefaults,
  getCurrentUser,
  getVoiceCatalogue,
  listProjects,
} from "@/lib/api";
import { emptyProductionConfig } from "@/components/production-config-form";
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
    getProjectStatus: vi.fn(),
    getVoiceCatalogue: vi.fn(),
    listProjects: vi.fn(),
    submitRun: vi.fn(),
  };
});

const DRAFT_KEY = "cinemora:create-draft:v1";
const SCRIPT = "The container ship changed everything it touched.";
const TITLE = "The Box";

function seedDraft() {
  const config = emptyProductionConfig();
  config.narration.voice_id = "en_US-lessac-medium";
  window.localStorage.setItem(
    DRAFT_KEY,
    JSON.stringify({
      script: SCRIPT,
      title: TITLE,
      requirePlanApproval: false,
      productionConfig: config,
    }),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getCurrentUser).mockResolvedValue(makeUser("user"));
  vi.mocked(listProjects).mockResolvedValue({ projects: [] } as never);
  vi.mocked(getBillingSummary).mockRejectedValue(new Error("no billing"));
  vi.mocked(checkApiConnection).mockResolvedValue({
    state: "connected",
    message: "Authenticated API connection confirmed.",
  });
  vi.mocked(getVoiceCatalogue).mockResolvedValue({
    voices: [
      {
        id: "en_US-lessac-medium",
        is_default: true,
        alias_of: null,
        model: "en_US-lessac-medium.onnx",
        model_file: "en_US-lessac-medium.onnx",
        config_file: "en_US-lessac-medium.onnx.json",
        installed: true,
      },
    ],
  } as never);
  // Config catalogue unreachable: the page falls back to standard
  // settings (labelled as such) and never blocks creation.
  vi.mocked(getConfigCatalogue).mockRejectedValue(new Error("no catalogue"));
  vi.mocked(getConfigDefaults).mockRejectedValue(new Error("no defaults"));
});

describe("Create page draft restore", () => {
  it("restores a saved draft into the form and shows the indicator", async () => {
    seedDraft();
    render(<CreatePage />);
    expect(
      await screen.findByText(/Draft restored from this browser\./),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Documentary script")).toHaveValue(SCRIPT);
    expect(screen.getByLabelText(/TITLE/)).toHaveValue(TITLE);
  });

  it("Discard removes the stored draft and empties the form", async () => {
    seedDraft();
    render(<CreatePage />);
    const discard = await screen.findByRole("button", {
      name: "Discard draft",
    });
    fireEvent.click(discard);
    expect(
      screen.queryByText(/Draft restored from this browser\./),
    ).toBeNull();
    expect(screen.getByLabelText("Documentary script")).toHaveValue("");
    expect(screen.getByLabelText(/TITLE/)).toHaveValue("");
    expect(window.localStorage.getItem(DRAFT_KEY)).toBeNull();
  });

  it("shows no indicator and an empty form when no draft is stored", async () => {
    render(<CreatePage />);
    // Wait for the page's async seeding to settle.
    expect(
      await screen.findByText(/standard settings/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Draft restored from this browser\./),
    ).toBeNull();
    expect(screen.getByLabelText("Documentary script")).toHaveValue("");
  });

  it("persists what the user types as a new draft (debounced)", async () => {
    render(<CreatePage />);
    await screen.findByText(/standard settings/);
    fireEvent.change(screen.getByLabelText("Documentary script"), {
      target: { value: "A brand new script about lighthouses." },
    });
    await waitFor(
      () => {
        const raw = window.localStorage.getItem(DRAFT_KEY);
        expect(raw).not.toBeNull();
        expect(JSON.parse(raw!).script).toBe(
          "A brand new script about lighthouses.",
        );
      },
      { timeout: 3000 },
    );
  });

  it("a corrupt stored draft is ignored: empty form, no indicator, no crash", async () => {
    window.localStorage.setItem(DRAFT_KEY, "{corrupt");
    render(<CreatePage />);
    expect(
      await screen.findByText(/standard settings/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Draft restored from this browser\./),
    ).toBeNull();
    expect(screen.getByLabelText("Documentary script")).toHaveValue("");
  });
});
