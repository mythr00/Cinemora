/**
 * lib/editor-gate (FS-F): the Studio gate predicate. Editable
 * only when a completed run AND the finished film exist — judged
 * from the runs list, NOT the headline project status, so a
 * project whose latest run failed after an earlier completed run
 * stays editable (pinned FS-F / A-10 behaviour). Admins are
 * exempt. lib/api is mocked at the module boundary.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { checkEditorGate, editorGateMessage } from "@/lib/editor-gate";
import {
  getCurrentUser,
  getOutputs,
  getProjectStatus,
  listRuns,
} from "@/lib/api";
import {
  makeOutputs,
  makeRun,
  makeStatus,
  makeUser,
} from "../helpers/fixtures";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getCurrentUser: vi.fn(),
    getOutputs: vi.fn(),
    getProjectStatus: vi.fn(),
    listRuns: vi.fn(),
  };
});

const mockUser = vi.mocked(getCurrentUser);
const mockOutputs = vi.mocked(getOutputs);
const mockStatus = vi.mocked(getProjectStatus);
const mockRuns = vi.mocked(listRuns);

beforeEach(() => {
  vi.clearAllMocks();
  mockUser.mockResolvedValue(makeUser("user"));
  mockStatus.mockResolvedValue(makeStatus({ status: "completed", latest_run: null }));
  mockOutputs.mockResolvedValue(makeOutputs(true));
  mockRuns.mockResolvedValue({ runs: [makeRun("completed")] });
});

describe("checkEditorGate", () => {
  it("is ready when a completed run exists and the film exists", async () => {
    const gate = await checkEditorGate("project-1");
    expect(gate.kind).toBe("ready");
  });

  it("stays editable when the latest run failed but an earlier run completed (FS-F/A-10)", async () => {
    mockRuns.mockResolvedValue({
      runs: [makeRun("failed", "run-new"), makeRun("completed", "run-old")],
    });
    mockStatus.mockResolvedValue(
      makeStatus({ status: "failed", latest_run: makeRun("failed", "run-new") }),
    );
    const gate = await checkEditorGate("project-1");
    expect(gate.kind).toBe("ready");
  });

  it("is not ready when the film is missing even with a completed run", async () => {
    mockOutputs.mockResolvedValue(makeOutputs(false));
    const gate = await checkEditorGate("project-1");
    expect(gate.kind).toBe("not-ready");
  });

  it("is not ready when the film exists but no run has completed", async () => {
    mockRuns.mockResolvedValue({ runs: [makeRun("running")] });
    mockStatus.mockResolvedValue(makeStatus({ status: "running", latest_run: makeRun("running") }));
    const gate = await checkEditorGate("project-1");
    expect(gate).toMatchObject({ kind: "not-ready", failed: false });
  });

  it("flags the failure framing when the only run failed and nothing completed", async () => {
    mockRuns.mockResolvedValue({ runs: [makeRun("failed")] });
    mockStatus.mockResolvedValue(
      makeStatus({ status: "failed", latest_run: makeRun("failed") }),
    );
    mockOutputs.mockResolvedValue(makeOutputs(false));
    const gate = await checkEditorGate("project-1");
    expect(gate).toMatchObject({ kind: "not-ready", failed: true });
  });

  it("does not use the headline project status as proof of completion", async () => {
    // Headline says completed, but the runs list has no completed
    // run and no film: the gate must stay closed.
    mockRuns.mockResolvedValue({ runs: [] });
    mockOutputs.mockResolvedValue(makeOutputs(false));
    mockStatus.mockResolvedValue(makeStatus({ status: "completed", latest_run: null }));
    const gate = await checkEditorGate("project-1");
    expect(gate.kind).toBe("not-ready");
  });

  it("exempts admins without needing a completed run or film", async () => {
    mockUser.mockResolvedValue(makeUser("admin"));
    mockRuns.mockResolvedValue({ runs: [] });
    mockOutputs.mockResolvedValue(makeOutputs(false));
    const gate = await checkEditorGate("project-1");
    expect(gate.kind).toBe("admin");
  });

  it("exempts super_admins as well", async () => {
    mockUser.mockResolvedValue(makeUser("super_admin"));
    const gate = await checkEditorGate("project-1");
    expect(gate.kind).toBe("admin");
  });

  it("fails closed when every API read fails", async () => {
    mockUser.mockRejectedValue(new Error("offline"));
    mockStatus.mockRejectedValue(new Error("offline"));
    mockOutputs.mockRejectedValue(new Error("offline"));
    mockRuns.mockRejectedValue(new Error("offline"));
    const gate = await checkEditorGate("project-1");
    expect(gate).toMatchObject({ kind: "not-ready", failed: false });
  });
});

describe("editorGateMessage", () => {
  it("uses the failure framing only for the failed gate", () => {
    expect(editorGateMessage({ failed: true }).body).toContain(
      "Video generation failed",
    );
    expect(editorGateMessage({ failed: false }).body).toContain(
      "Editing opens when generation completes",
    );
    expect(editorGateMessage({ failed: true }).heading).toBe(
      "Your video isn't ready yet",
    );
  });
});
