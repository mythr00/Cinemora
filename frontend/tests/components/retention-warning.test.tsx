/**
 * RetentionWarning (§21, Phase 8 MON): the project page's
 * deletion warning. It must render the SERVER's headline, the
 * exact expiry timestamp, a live countdown ticking down from
 * the server's delete_after, and a prominent Download action —
 * and render nothing at all when there is no active video.
 * (Component extracted verbatim from app/projects/[id]/page.tsx
 * in Phase 9; behaviour unchanged.)
 */
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RetentionWarning } from "@/components/retention-warning";
import { makeRetention } from "../helpers/fixtures";

// Fixed "now": 2026-11-25T18:00:00Z. The fixture's delete_after
// is 2026-11-27T20:00:00Z — exactly 50h later.
const NOW = new Date("2026-11-25T18:00:00.000Z").getTime();
const DELETE_AFTER = "2026-11-27T20:00:00.000Z";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

function renderWarning(retention = makeRetention()) {
  return render(
    <RetentionWarning
      retention={retention}
      filmPath="/media/final.mp4"
      filmFilename="final.mp4"
    />,
  );
}

describe("RetentionWarning", () => {
  it("renders the server headline verbatim", () => {
    renderWarning(makeRetention({ headline: "Server headline XYZ" }));
    expect(
      screen.getByRole("heading", { name: "Server headline XYZ" }),
    ).toBeInTheDocument();
  });

  it("renders the exact expiry timestamp from the server data", () => {
    renderWarning();
    const expected = new Date(DELETE_AFTER).toLocaleString();
    expect(
      screen.getByText(new RegExp(`permanently deleted on`)),
    ).toBeInTheDocument();
    expect(screen.getByText(expected)).toBeInTheDocument();
  });

  it("shows a live countdown that ticks down every second", () => {
    renderWarning();
    expect(screen.getByText("50h 00m 00s remaining")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(61_000);
    });
    expect(screen.getByText("49h 58m 59s remaining")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(3_600_000);
    });
    expect(screen.getByText("48h 58m 59s remaining")).toBeInTheDocument();
  });

  it("clamps the countdown at zero after the deadline", () => {
    renderWarning();
    act(() => {
      vi.advanceTimersByTime(51 * 3_600_000);
    });
    expect(screen.getByText("0h 00m 00s remaining")).toBeInTheDocument();
  });

  it("offers a prominent Download action for the film", () => {
    renderWarning();
    const download = screen.getByRole("button", {
      name: "Download your video now",
    });
    expect(download).toBeInTheDocument();
    expect(download).toHaveClass("button-primary");
  });

  it("says the video has not been downloaded yet when the server says so", () => {
    renderWarning();
    expect(
      screen.getByText("You have not downloaded this video yet."),
    ).toBeInTheDocument();
  });

  it("reports the server's download count once downloaded", () => {
    const retention = makeRetention();
    retention.videos[0] = {
      ...retention.videos[0],
      downloaded: true,
      download_count: 3,
    };
    renderWarning(retention);
    expect(
      screen.getByText(/Downloaded 3 times — keep your copy safe\./),
    ).toBeInTheDocument();
  });

  it("renders null when retention data is absent", () => {
    const { container } = render(
      <RetentionWarning retention={null} filmPath="" filmFilename="" />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders null when no video is active", () => {
    const retention = makeRetention();
    retention.videos[0] = { ...retention.videos[0], status: "deleted" };
    const { container } = renderWarning(retention);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders null when the registry has no videos at all", () => {
    const { container } = renderWarning(makeRetention({ videos: [] }));
    expect(container).toBeEmptyDOMElement();
  });

  it("prefers the active final video over other active kinds", () => {
    const retention = makeRetention();
    const trailer = {
      ...retention.videos[0],
      id: "video-trailer",
      kind: "trailer",
      delete_after: "2026-11-26T18:00:00.000Z",
    };
    retention.videos = [trailer, retention.videos[0]];
    renderWarning(retention);
    // Countdown is to the FINAL video's deadline (50h), not the trailer's (24h).
    expect(screen.getByText("50h 00m 00s remaining")).toBeInTheDocument();
  });
});
