id: certificate
title: Production certificate and quality verdicts
link: /projects/[id]/certificate
keywords: certificate, provenance, quality, verdicts, trust, evidence, report

The Certificate tab (/projects/[id]/certificate) is the
production's provenance record for its latest run. It states:

- Which assets were used, with their sources, verification
  verdicts and rights/licence evidence (the ledger certificate).
- The planner block: which planner produced the plan (full or
  degraded mode) and, in degraded mode, the recorded reason.
- The quality section: the production's recorded quality report —
  integrity, coverage and reliability verdicts (PASS,
  QUALITY_WARNING, FAIL or UNKNOWN) with the measurements behind
  them, reconciled across the run's artefacts.

A verdict of UNKNOWN or a "not recorded" quality section is an
honest state, not an error: it means that measurement was not
produced for that run (for example, a production that predates
the quality report). The certificate describes the original
production only — editor revisions have their own certificates,
so editing can never rewrite the original's record.
