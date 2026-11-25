/**
 * lib/draft-storage (Phase 6, A-05): local draft persistence for
 * creation forms. Contract under test: a save/load round-trip
 * restores everything the create form persists (script, title,
 * config, voice); corrupt data and storage failures fail soft —
 * load returns null and no helper ever throws.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearDraft, loadDraft, saveDraft } from "@/lib/draft-storage";

const KEY = "cinemora:create-draft:v1";

type CreateDraft = {
  script?: string;
  title?: string;
  requirePlanApproval?: boolean;
  productionConfig?: { narration: { voice_id: string | null } };
};

const draft: CreateDraft = {
  script: "In 1969, a container ship changed world trade forever.",
  title: "The Box That Moved the World",
  requirePlanApproval: true,
  productionConfig: { narration: { voice_id: "en_US-lessac-medium" } },
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("draft-storage round-trip", () => {
  it("restores script, title, config and voice exactly as saved", () => {
    saveDraft(KEY, draft);
    const restored = loadDraft<CreateDraft>(KEY);
    expect(restored).toEqual(draft);
    expect(restored?.script).toBe(draft.script);
    expect(restored?.title).toBe(draft.title);
    expect(restored?.productionConfig?.narration.voice_id).toBe(
      "en_US-lessac-medium",
    );
  });

  it("returns null when nothing was stored", () => {
    expect(loadDraft(KEY)).toBeNull();
  });

  it("keeps drafts under different keys isolated", () => {
    saveDraft(KEY, draft);
    expect(loadDraft("cinemora:other-draft")).toBeNull();
  });

  it("overwrites an earlier draft with the latest save", () => {
    saveDraft(KEY, { ...draft, title: "First title" });
    saveDraft(KEY, { ...draft, title: "Second title" });
    expect(loadDraft<CreateDraft>(KEY)?.title).toBe("Second title");
  });
});

describe("draft-storage fail-soft behaviour", () => {
  it("returns null for corrupt stored data instead of throwing", () => {
    window.localStorage.setItem(KEY, "{not valid json");
    expect(() => loadDraft(KEY)).not.toThrow();
    expect(loadDraft(KEY)).toBeNull();
  });

  it("returns null when reading storage itself throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage unavailable");
    });
    expect(loadDraft(KEY)).toBeNull();
  });

  it("never throws when saving hits a storage quota error", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota exceeded", "QuotaExceededError");
    });
    expect(() => saveDraft(KEY, draft)).not.toThrow();
  });

  it("a failed save leaves no partial draft behind", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota exceeded", "QuotaExceededError");
    });
    saveDraft(KEY, draft);
    vi.restoreAllMocks();
    expect(loadDraft(KEY)).toBeNull();
  });
});

describe("draft-storage clear", () => {
  it("clear removes the draft so a later load returns null", () => {
    saveDraft(KEY, draft);
    expect(loadDraft(KEY)).not.toBeNull();
    clearDraft(KEY);
    expect(loadDraft(KEY)).toBeNull();
  });

  it("clear never throws when storage removal fails", () => {
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("storage unavailable");
    });
    expect(() => clearDraft(KEY)).not.toThrow();
  });

  it("clearing a key that was never saved is a no-op", () => {
    expect(() => clearDraft("cinemora:never-saved")).not.toThrow();
    expect(loadDraft("cinemora:never-saved")).toBeNull();
  });
});
