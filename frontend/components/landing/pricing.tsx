"use client";

import { useState } from "react";
import Link from "next/link";
import { ArrowUpRight, Check } from "lucide-react";

/**
 * Landing pricing (redesign 2026-11-26, directive Phase 7;
 * spacious pass + founder credit decision 2026-11-26).
 * Figures are the founder-decided tiers: Creator $29, Studio
 * $79, Agency $179 per month; subscription credits 1,300 /
 * 3,600 / 7,200 (founder decision 2026-11-26, superseding the
 * monetisation directive's original credit figures); annual
 * billing is 20% less, charged upfront, with credits still
 * distributed monthly. There is no free plan. Every plan runs
 * the same production engine and quality — plans differ in
 * allowances, concurrency, priority and collaboration only.
 * Documentary counts are the plans' included standard
 * 15-minute generations.
 *
 * Reference (strikethrough) prices: `referenceMonthly` /
 * `referenceAnnualPerMonth` render only when set, and they are
 * deliberately UNSET. A value may be entered here only when it
 * mirrors a genuine reference price approved by the founder and
 * recorded in the admin plan configuration (the billing page
 * reads the same convention from the plan record's
 * features.reference_price_*_cents). The illustrative figures
 * in the redesign directive were never approved and must not
 * be entered.
 */

type Plan = {
  name: string;
  monthly: number;
  annualPerMonth: string;
  referenceMonthly?: number;
  referenceAnnualPerMonth?: string;
  popular?: boolean;
  blurb: string;
  credits: string;
  documentaries: string;
  extras: string[];
};

const PLANS: Plan[] = [
  {
    name: "Creator",
    monthly: 29,
    annualPerMonth: "23.20",
    blurb: "For a channel getting moving — a few documentaries a month, produced properly.",
    credits: "1,300 credits / month",
    documentaries: "11 standard 15-min documentaries included",
    extras: ["1 production at a time", "Script & thumbnail allowances included"],
  },
  {
    name: "Studio",
    monthly: 79,
    annualPerMonth: "63.20",
    popular: true,
    blurb: "For regular publishers who need a steady rhythm of finished films.",
    credits: "3,600 credits / month",
    documentaries: "29 standard 15-min documentaries included",
    extras: ["Higher concurrency", "Priority rendering queue"],
  },
  {
    name: "Agency",
    monthly: 179,
    annualPerMonth: "143.20",
    blurb: "For teams and networks producing across several channels at once.",
    credits: "7,200 credits / month",
    documentaries: "59 standard 15-min documentaries included",
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
          <article className={plan.popular ? "price-card price-card--popular" : "price-card"} key={plan.name}>
            {plan.popular && <span className="price-badge">Most popular</span>}
            <h3>{plan.name}</h3>
            <p className="price-value">
              {annual
                ? plan.referenceAnnualPerMonth
                  ? <s className="price-reference">${plan.referenceAnnualPerMonth}</s>
                  : null
                : plan.referenceMonthly
                  ? <s className="price-reference">${plan.referenceMonthly}</s>
                  : null}
              <strong>${annual ? plan.annualPerMonth : plan.monthly}</strong>
              <span> / month{annual ? ", billed annually" : ""}</span>
            </p>
            <p className="price-blurb">{plan.blurb}</p>
            <ul className="price-list">
              <li><Check size={14} strokeWidth={2.4} aria-hidden /> {plan.credits}</li>
              <li><Check size={14} strokeWidth={2.4} aria-hidden /> {plan.documentaries}</li>
              {plan.extras.map((extra) => (
                <li key={extra}><Check size={14} strokeWidth={2.4} aria-hidden /> {extra}</li>
              ))}
            </ul>
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
        Prices in USD. Annual plans are charged upfront for the year; credits are still
        distributed monthly. Need more mid-month? Top-up credits are available in the app and
        stay valid for 12 months. No free plan — every plan is the full studio.
      </p>
    </div>
  );
}
