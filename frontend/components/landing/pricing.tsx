"use client";

import { useState } from "react";
import Link from "next/link";
import { ArrowUpRight, Check } from "lucide-react";

/**
 * Landing pricing (pricing restructure, founder directive
 * 2026-11-26 ~10:25 WAT; presentation per the pricing page
 * correction, founder directive 2026-11-26 21:48 WAT).
 * Creator $28, Studio $63, Agency $111 per month; video
 * credits 990 / 2,325 / 3,660; each plan's credits cover its
 * included 11 / 31 / 61 standard 15-minute documentaries, and
 * script and thumbnail allowances (11 / 31 / 61 each) are
 * tracked separately from the video credits. Annual billing
 * is 20% less, charged upfront, with credits still
 * distributed monthly. There is no free plan. Every plan runs
 * the same production engine and quality — plans differ in
 * allowances, concurrency, priority and collaboration only.
 * These figures mirror the backend billing catalogue
 * (services/billing/catalogue.py) and are pinned against it
 * by tests/components/landing-pricing.test.tsx.
 *
 * Crossed-out prices are the genuine previous plan prices —
 * $29 / $79 / $179 per month before the 2026-11-26
 * restructure (annual equivalents at the same −20% annual
 * structure) — never invented reference figures.
 *
 * There is deliberately NO plan comparison table and no
 * per-minute rate or credit-conversion mathematics on this
 * page (founder correction, 2026-11-26): customers see what a
 * plan costs, what they receive each month, and what they
 * can create.
 */

type Plan = {
  name: string;
  monthly: number;
  annualPerMonth: string;
  referenceMonthly: number;
  referenceAnnualPerMonth: string;
  bestValue?: boolean;
  blurb: string;
  credits: string;
  perVideo: string;
  operations: string;
  everythingIn?: string;
  extras: string[];
};

const PLANS: Plan[] = [
  {
    name: "Creator",
    monthly: 28,
    annualPerMonth: "22.40",
    referenceMonthly: 29,
    referenceAnnualPerMonth: "23.20",
    blurb: "For a channel getting moving — a few documentaries a month, produced properly.",
    credits: "990 video credits / month — 11 documentaries at 15 minutes each",
    perVideo: "≈ $2.55 per finished video",
    operations: "11 AI scripts & 11 AI thumbnails included",
    extras: ["1 production at a time"],
  },
  {
    name: "Studio",
    monthly: 63,
    annualPerMonth: "50.40",
    referenceMonthly: 79,
    referenceAnnualPerMonth: "63.20",
    blurb: "A steady upload rhythm — the pace that grows channels.",
    credits: "2,325 video credits / month — 31 documentaries at 15 minutes each",
    perVideo: "≈ $2.03 per finished video",
    operations: "31 AI scripts & 31 AI thumbnails included",
    everythingIn: "Creator",
    extras: ["Higher concurrency", "Priority rendering queue"],
  },
  {
    name: "Agency",
    monthly: 111,
    annualPerMonth: "88.80",
    referenceMonthly: 179,
    referenceAnnualPerMonth: "143.20",
    bestValue: true,
    blurb: "For teams and networks producing across several channels at once.",
    credits: "3,660 video credits / month — 61 documentaries at 15 minutes each",
    perVideo: "≈ $1.82 per finished video",
    operations: "61 AI scripts & 61 AI thumbnails included",
    everythingIn: "Studio",
    extras: ["Highest concurrency & priority", "Team workspace & collaboration"],
  },
];

const SHARED = [
  "The same production engine and quality on every plan",
  "Per-sentence visual planning & plan review",
  "Motion graphics, narration, word-synced captions & music",
  "Revision editing — the original is never overwritten",
  "Provenance certificate & rights manifest per production",
];

export function LandingPricing() {
  const [annual, setAnnual] = useState(false);
  return (
    <div className="pricing-wrap">
      <div className="pricing-toggle" role="group" aria-label="Billing period">
        <button type="button" aria-pressed={!annual} className={!annual ? "is-active" : ""} onClick={() => setAnnual(false)}>Monthly</button>
        <button type="button" aria-pressed={annual} className={annual ? "is-active" : ""} onClick={() => setAnnual(true)}>Annual <span>−20%</span></button>
      </div>
      <div className="pricing-grid">
        {PLANS.map((plan) => (
          <article className={plan.bestValue ? "price-card price-card--popular" : "price-card"} key={plan.name}>
            {plan.bestValue && <span className="price-badge">Best value</span>}
            <h3>{plan.name}</h3>
            <p className="price-value">
              <s className="price-reference">${annual ? plan.referenceAnnualPerMonth : plan.referenceMonthly}</s>
              <strong>${annual ? plan.annualPerMonth : plan.monthly}</strong>
              <span> / month{annual ? ", billed annually" : ""}</span>
            </p>
            <p className="price-per-video">{plan.perVideo}</p>
            <p className="price-blurb">{plan.blurb}</p>
            <ul className="price-list">
              <li><Check size={14} strokeWidth={2.4} aria-hidden /> {plan.credits}</li>
              <li><Check size={14} strokeWidth={2.4} aria-hidden /> {plan.operations}</li>
              {plan.extras.map((extra) => (
                <li key={extra}><Check size={14} strokeWidth={2.4} aria-hidden /> {extra}</li>
              ))}
            </ul>
            {plan.everythingIn && (
              <p className="price-everything">Everything in {plan.everythingIn}</p>
            )}
            <Link className="button button-primary price-cta" href="/register">Create account <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>
          </article>
        ))}
      </div>
      <ul className="pricing-shared">
        {SHARED.map((item) => (
          <li key={item}><Check size={14} strokeWidth={2.4} aria-hidden /> {item}</li>
        ))}
      </ul>
      <p className="pricing-footnote">
        Prices in USD. You are charged for the minutes of finished video you actually
        receive — failed generations are always fully refunded. Script and thumbnail
        allowances are separate from video credits. Annual plans are charged upfront
        for the year; credits are still distributed monthly. Need more mid-month?
        Top-up credits are available in the app and stay valid for 12 months.
        No free plan — every plan is the full studio.
      </p>
    </div>
  );
}
