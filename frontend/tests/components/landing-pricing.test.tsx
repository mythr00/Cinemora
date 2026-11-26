/**
 * Landing pricing (Frontend Redesign 2026-11-26): the decided
 * tiers with the founder-decided display credits (1,300 / 3,600
 * / 7,200, decision 2026-11-26), the Studio popular-plan badge,
 * the annual −20% toggle — and the pricing-integrity rule: no
 * strikethrough reference price renders while no genuine
 * reference price is configured.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LandingPricing } from "@/components/landing/pricing";

describe("LandingPricing", () => {
  it("shows the decided prices and founder-decided credits", () => {
    render(<LandingPricing />);
    expect(screen.getByText("$29")).toBeTruthy();
    expect(screen.getByText("$79")).toBeTruthy();
    expect(screen.getByText("$179")).toBeTruthy();
    expect(screen.getByText("1,300 credits / month")).toBeTruthy();
    expect(screen.getByText("3,600 credits / month")).toBeTruthy();
    expect(screen.getByText("7,200 credits / month")).toBeTruthy();
    expect(screen.getByText("11 standard 15-min documentaries included")).toBeTruthy();
    expect(screen.getByText("29 standard 15-min documentaries included")).toBeTruthy();
    expect(screen.getByText("59 standard 15-min documentaries included")).toBeTruthy();
  });

  it("badges Studio as the popular plan only", () => {
    const { container } = render(<LandingPricing />);
    expect(screen.getByText("Most popular")).toBeTruthy();
    const popular = container.querySelectorAll(".price-card--popular");
    expect(popular.length).toBe(1);
    expect(popular[0].textContent).toContain("Studio");
  });

  it("renders no strikethrough reference price while unconfigured", () => {
    const { container } = render(<LandingPricing />);
    expect(container.querySelectorAll(".price-reference").length).toBe(0);
    expect(container.querySelectorAll("s").length).toBe(0);
  });

  it("annual toggle shows the −20% per-month prices", () => {
    render(<LandingPricing />);
    fireEvent.click(screen.getByRole("button", { name: /Annual/ }));
    expect(screen.getByText("$23.20")).toBeTruthy();
    expect(screen.getByText("$63.20")).toBeTruthy();
    expect(screen.getByText("$143.20")).toBeTruthy();
  });
});
