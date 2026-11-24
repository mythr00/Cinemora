id: asset-manager
title: Asset Manager — how visuals are chosen and verified
link: /projects/[id]/assets
keywords: asset manager, assets, decisions, verification, verified, rejected, rights, licence, footage, images

The Asset Manager tab (/projects/[id]/assets) is the decision
ledger for a production: every visual the pipeline considered for
the latest run, what it decided, and why.

For each candidate asset you can see:

- The decision: accepted (used in the film), rejected, or missing
  (no verified visual could be found for that sentence).
- The verification verdict: the checks the asset passed — topical
  match, frame-level verification and OCR where applicable — and,
  for rejections, the recorded reason (for example a contradictory
  place or subject, or a named person who is not the subject).
- Rights: the licence evidence recorded for the asset. Where a
  provider supplies licence metadata (Wikimedia Commons does), it
  is recorded verbatim; where it does not, the licence honestly
  stays "unknown" with a verify-before-publish status.

Discovery searches the web, images and videos (Serper), Wikimedia
Commons, Archive.org and YouTube. Candidates whose metadata merely
fails to mention the required subject are not automatically
rejected — they can be admitted as speculative candidates, but
only become usable when frame verification confirms them.

The Asset Manager is read-only evidence: to change a visual, use
the Plan tab's replace decision, or re-run the production.
