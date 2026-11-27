/**
 * Landing pricing (pricing restructure, founder directive
 * 2026-11-26 ~10:25 WAT; presentation per the pricing page
 * correction, founder directive 2026-11-26 21:48 WAT):
 * Creator $29 / Studio $63 / Agency $111; video credits
 * 990 / 2,325 / 3,660 covering 11 / 31 / 61 standard 15-min
 * videos; Agency badged Best value; the annual −20% toggle.
 * The correction requires: clean cards only — NO plan
 * comparison table anywhere, no per-minute rates or credit
 * mathematics — and a crossed-out reference price
 * ($39 / $79 / $179) beside each current price. Creator's
 * $29 price and $39 reference were set by the founder on
 * 2026-11-27. The figures mirror the backend billing
 * catalogue (services/billing/catalogue.py).
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LandingPricing } from "@/components/landing/pricing";

describe("LandingPricing", () => {
  it("shows the restructured prices, credits and video counts", () => {
    render(<LandingPricing />);
    expect(screen.getByText("$29")).toBeTruthy();
    expect(screen.getByText("$63")).toBeTruthy();
    expect(screen.getByText("$111")).toBeTruthy();
    expect(
      screen.getByText("990 video credits / month — 11 documentaries at 15 minutes each"),
    ).toBeTruthy();
    expect(
      screen.getByText("2,325 video credits / month — 31 documentaries at 15 minutes each"),
    ).toBeTruthy();
    expect(
      screen.getByText("3,660 video credits / month — 61 documentaries at 15 minutes each"),
    ).toBeTruthy();
    expect(screen.getByText("11 AI scripts & 11 AI thumbnails included")).toBeTruthy();
    expect(screen.getByText("31 AI scripts & 31 AI thumbnails included")).toBeTruthy();
    expect(screen.getByText("61 AI scripts & 61 AI thumbnails included")).toBeTruthy();
  });

  it("shows the approximate price per finished video and nothing more mathematical", () => {
    const { container } = render(<LandingPricing />);
    expect(screen.getByText("≈ $2.64 per finished video")).toBeTruthy();
    expect(screen.getByText("≈ $2.03 per finished video")).toBeTruthy();
    expect(screen.getByText("≈ $1.82 per finished video")).toBeTruthy();
    // No credit-conversion mathematics on the page.
    expect(container.textContent).not.toMatch(/credits per finished minute/);
    expect(container.textContent).not.toMatch(/15-min documentary: \d+ credits/);
  });

  it("has no plan comparison table or duplicate comparison block", () => {
    const { container } = render(<LandingPricing />);
    expect(container.querySelectorAll("table").length).toBe(0);
    expect(screen.queryByText("Plan comparison")).toBeNull();
    expect(screen.queryByText("Standard 15-min videos")).toBeNull();
    expect(screen.queryByText("Credits per finished minute")).toBeNull();
  });

  it("badges Agency as best value only", () => {
    const { container } = render(<LandingPricing />);
    expect(screen.getByText("Best value")).toBeTruthy();
    expect(screen.queryByText("Most popular")).toBeNull();
    const highlighted = container.querySelectorAll(".price-card--popular");
    expect(highlighted.length).toBe(1);
    expect(highlighted[0].textContent).toContain("Agency");
  });

  it("crosses out the reference prices beside the current ones", () => {
    const { container } = render(<LandingPricing />);
    const struck = Array.from(container.querySelectorAll("s.price-reference")).map(
      (node) => node.textContent,
    );
    expect(struck).toEqual(["$39", "$79", "$179"]);
    // Real line-through styling, no tilde characters anywhere.
    expect(container.textContent).not.toContain("~");
  });

  it("annual toggle shows the −20% prices with the previous annual prices struck", () => {
    const { container } = render(<LandingPricing />);
    fireEvent.click(screen.getByRole("button", { name: /Annual/ }));
    expect(screen.getByText("$23.20")).toBeTruthy();
    expect(screen.getByText("$50.40")).toBeTruthy();
    expect(screen.getByText("$88.80")).toBeTruthy();
    const struck = Array.from(container.querySelectorAll("s.price-reference")).map(
      (node) => node.textContent,
    );
    expect(struck).toEqual(["$31.20", "$63.20", "$143.20"]);
  });

  it("carries the Everything-in lines for the upgrade path", () => {
    render(<LandingPricing />);
    expect(screen.getByText("Everything in Creator")).toBeTruthy();
    expect(screen.getByText("Everything in Studio")).toBeTruthy();
  });
});
