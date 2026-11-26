/**
 * Landing pricing (pricing restructure, founder directive
 * 2026-11-26 ~10:25 WAT): Creator $28 / Studio $63 / Agency
 * $111; video credits 990 / 2,325 / 3,660; 11 / 31 / 61 standard
 * 15-min videos; per-minute rates 6 / 5 / 4; Agency badged Best
 * value; the annual −20% toggle — and the pricing-integrity
 * rule: no strikethrough reference price renders while no
 * genuine reference price is configured. The figures mirror the
 * backend billing catalogue (services/billing/catalogue.py).
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LandingPricing } from "@/components/landing/pricing";

describe("LandingPricing", () => {
  it("shows the restructured prices, credits and video counts", () => {
    render(<LandingPricing />);
    expect(screen.getByText("$28")).toBeTruthy();
    expect(screen.getByText("$63")).toBeTruthy();
    expect(screen.getByText("$111")).toBeTruthy();
    expect(screen.getByText("990 video credits / month")).toBeTruthy();
    expect(screen.getByText("2,325 video credits / month")).toBeTruthy();
    expect(screen.getByText("3,660 video credits / month")).toBeTruthy();
    expect(screen.getByText("11 standard 15-min documentaries / month")).toBeTruthy();
    expect(screen.getByText("31 standard 15-min documentaries / month")).toBeTruthy();
    expect(screen.getByText("61 standard 15-min documentaries / month")).toBeTruthy();
  });

  it("shows per-minute rates, the 15-min cost and price per video", () => {
    render(<LandingPricing />);
    expect(screen.getByText("6 credits per finished minute")).toBeTruthy();
    expect(screen.getByText("5 credits per finished minute")).toBeTruthy();
    expect(screen.getByText("4 credits per finished minute")).toBeTruthy();
    expect(screen.getByText(/90 credits · ≈\$2\.55 per video/)).toBeTruthy();
    expect(screen.getByText(/75 credits · ≈\$2\.03 per video/)).toBeTruthy();
    expect(screen.getByText(/60 credits · ≈\$1\.82 per video/)).toBeTruthy();
    expect(screen.getByText("11 AI scripts & 11 AI thumbnails included")).toBeTruthy();
    expect(screen.getByText("31 AI scripts & 31 AI thumbnails included")).toBeTruthy();
    expect(screen.getByText("61 AI scripts & 61 AI thumbnails included")).toBeTruthy();
  });

  it("comparison table carries every required row", () => {
    render(<LandingPricing />);
    for (const label of [
      "Price",
      "Video credits",
      "Standard 15-min videos",
      "Price per video",
      "AI scripts included",
      "AI thumbnails included",
      "Credits per finished minute",
    ]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
  });

  it("badges Agency as best value only", () => {
    const { container } = render(<LandingPricing />);
    expect(screen.getByText("Best value")).toBeTruthy();
    expect(screen.queryByText("Most popular")).toBeNull();
    const highlighted = container.querySelectorAll(".price-card--popular");
    expect(highlighted.length).toBe(1);
    expect(highlighted[0].textContent).toContain("Agency");
  });

  it("renders no strikethrough reference price while unconfigured", () => {
    const { container } = render(<LandingPricing />);
    expect(container.querySelectorAll(".price-reference").length).toBe(0);
    expect(container.querySelectorAll("s").length).toBe(0);
  });

  it("annual toggle shows the −20% per-month prices", () => {
    render(<LandingPricing />);
    fireEvent.click(screen.getByRole("button", { name: /Annual/ }));
    expect(screen.getByText("$22.40")).toBeTruthy();
    expect(screen.getByText("$50.40")).toBeTruthy();
    expect(screen.getByText("$88.80")).toBeTruthy();
  });
});
