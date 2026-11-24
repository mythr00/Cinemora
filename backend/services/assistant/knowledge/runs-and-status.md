id: runs-and-status
title: Runs, statuses, retry and cancellation
link: /projects
keywords: run, status, queued, running, failed, cancelled, retry, resume, cancel, heartbeat, stuck

Every production submission creates a run, recorded in the run
ledger with its status, current stage, options and script hash.

- queued: waiting for a worker to claim it.
- running: a worker is producing it; the run heartbeats while it
  works, and the assistant can report the heartbeat's age and
  whether the run looks live.
- awaiting_approval: analysis finished; the run is parked until
  the plan is approved on the Plan tab.
- completed: the film was produced and delivered.
- failed: the run ended with an error, recorded on the run and in
  the stage records.
- cancelled: cancellation was requested and the run stopped at a
  checkpoint.

Retry: submitting the same project again after a failure creates
a new run. If the script is unchanged, stages the failed run
completed are adopted after artefact validation (stage resume),
and the new run's records say which stages were adopted and which
ran fresh; if adoption is not possible, the fallback reason is
recorded.

Cancellation: requesting a cancel flags the run; the worker stops
at its next checkpoint. Cancelling destroys in-progress work,
which is why the assistant treats it as a consequential operation
that needs explicit confirmation.
