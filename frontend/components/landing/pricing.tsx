"use client";

import { useState } from "react";
import Link from "next/link";
import { ArrowUpRight, Check } from "lucide-react";

/**
 * Landing pricing (pricing restructure, founder directive
 * 2026-11-26 ~10:25 WAT — supersedes the same morning's earlier
 * figures). Creator $28, Studio $63, Agency $111 per month;
 * video credits 990 / 2,325 / 3,660; per-finished-minute rates
 * 6 / 5 / 4 credits, so a standard 15-minute documentary costs
 * 90 / 75 / 60 credits and each plan's credits exactly cover
 * its included 11 / 31 / 61 standard documentaries. Script and
 * thumbnail allowances (11 / 31 / 61 each) are tracked
 * separately from the video credits. Annual billing is 20%
 * less, charged upfront, with credits still distributed
 * monthly. There is no free plan. Every plan runs the same
 * production engine and quality — plans differ in allowances,
 * rates, concurrency, priority and collaboration only. These
 * figures mirror the backend billing catalogue
 * (services/billing/catalogue.py) and are pinned against it by
 * tests/components/landing-pricing.test.tsx.
 *
 * Reference (strikethrough) prices: `referenceMonthly` /
 * `referenceAnnualPerMonth` render only when set, and they are
 * deliberately UNSET. A value may be entered here only when it
 * mirrors a genuine reference price approved by the founder and
 * recorded in the admin plan configuration (the billing page
 * reads the same convention from the plan record's
 * features.reference_price_*_cents).
 */

type Plan = {
  name: string;
  monthly: number;
  annualPerMonth: string;
  referenceMonthly?: number;
  referenceAnnualPerMonth?: string;
  bestValue?: boolean;
  blurb: string;
  credits: string;
  documentaries: string;
  rate: string;
  standardCost: string;
  operations: string;
  extras: string[];
};

const PLANS: Plan[] = [
  {
    name: "Creator",
    monthly: 28,
    annualPerMonth: "22.40",
    blurb: "For a channel getting moving — a few documentaries a month, produced properly.",
    credits: "990 video credits / month",
    documentaries: "11 standard 15-min documentaries / month",
    rate: "6 credits per finished minute",
    standardCost: "15-min documentary: 90 credits · ≈$2.55 per video",
    operations: "11 AI scripts & 11 AI thumbnails included",
    extras: ["1 production at a time"],
  },
  {
    name: "Studio",
    monthly: 63,
    annualPerMonth: "50.40",
    blurb: "For regular publishers who need a steady rhythm of finished films.",
    credits: "2,325 video credits / month",
    documentaries: "31 standard 15-min documentaries / month",
    rate: "5 credits per finished minute",
    standardCost: "15-min documentary: 75 credits · ≈$2.03 per video",
    operations: "31 AI scripts & 31 AI thumbnails included",
    extras: ["Higher concurrency", "Priority rendering queue"],
  },
  {
    name: "Agency",
    monthly: 111,
    annualPerMonth: "88.80",
    bestValue: true,
    blurb: "For teams and networks producing across several channels at once.",
    credits: "3,660 video credits / month",
    documentaries: "61 standard 15-min documentaries / month",
    rate: "4 credits per finished minute",
    standardCost: "15-min documentary: 60 credits · ≈$1.82 per video",
    operations: "61 AI scripts & 61 AI thumbnails included",
    extras: ["Highest concurrency & priority", "Team workspace & collaboration"],
  },
];

const COMPARE_ROWS: { label: string; values: string[] }[] = [
  { label: "Price", values: ["$28 / month", "$63 / month", "$111 / month"] },
  { label: "Video credits", values: ["990 / month", "2,325 / month", "3,660 / month"] },
  { label: "Standard 15-min videos", values: ["11 / month", "31 / month", "61 / month"] },
  { label: "Price per video", values: ["≈$2.55", "≈$2.03", "≈$1.82"] },
  { label: "AI scripts included", values: ["11 / month", "31 / month", "61 / month"] },
  { label: "AI thumbnails included", values: ["11 / month", "31 / month", "61 / month"] },
  { label: "Credits per finished minute", values: ["6", "5", "4"] },
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
              <li><Check size={14} strokeWidth={2.4} aria-hidden /> {plan.rate}</li>
              <li><Check size={14} strokeWidth={2.4} aria-hidden /> {plan.standardCost}</li>
              <li><Check size={14} strokeWidth={2.4} aria-hidden /> {plan.operations}</li>
              {plan.extras.map((extra) => (
                <li key={extra}><Check size={14} strokeWidth={2.4} aria-hidden /> {extra}</li>
              ))}
            </ul>
            <Link className="button button-primary price-cta" href="/register">Create account <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>
          </article>
        ))}
      </div>
      <table className="pricing-compare">
        <caption>Plan comparison</caption>
        <thead>
          <tr>
            <th scope="col" aria-label="Feature"></th>
            {PLANS.map((plan) => (
              <th scope="col" key={plan.name}>{plan.name}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {COMPARE_ROWS.map((row) => (
            <tr key={row.label}>
              <th scope="row">{row.label}</th>
              {row.values.map((value, i) => (
                <td key={i}>{value}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <ul className="pricing-shared">
        {SHARED.map((item) => (
          <li key={item}><Check size={14} strokeWidth={2.4} aria-hidden /> {item}</li>
        ))}
      </ul>
      <p className="pricing-footnote">
        Prices in USD. You are charged for the minutes of finished video you actually
        receive, at your plan&apos;s per-minute rate — failed generations are always fully
        refunded. Script and thumbnail allowances are separate from video credits.
        Annual plans are charged upfront for the year; credits are still distributed
        monthly. Need more mid-month? Top-up credits are available in the app and stay
        valid for 12 months. No free plan — every plan is the full studio.
      </p>
    </div>
  );
}
