id: assistant-help
title: Using the Assistant
link: /assistant
keywords: assistant, help, confirm, confirmation, pending action, what can you do

The Assistant (/assistant) answers from your real Cinemora
records — projects, runs, asset decisions, reports — and can
search the web, YouTube, Wikimedia and Archive.org for resources,
returning real links from the providers.

Operations come in three classes:

- READ (automatic): questions about your account, projects,
  productions, failures, outputs and the product itself.
- WRITE (confirmation): creating a project, submitting or
  retrying a production run, approving a plan, recording a plan
  decision, requesting a revision render, saving a preference.
  The assistant prepares the exact operation and shows it to you;
  nothing runs until you press Confirm.
- CONSEQUENTIAL (explicit confirmation): cancelling a run. The
  confirmation card is marked as consequential and needs a
  deliberate two-step confirm.

Pending actions expire after 15 minutes. Rejecting or letting an
action expire means nothing was executed. Every tool call the
assistant makes — reads included — is recorded in your assistant
activity, and each answer shows the tools it used.

The assistant only ever sees your own data. It has no access to
external accounts: no Google Drive, YouTube or cloud-storage
connections exist in Cinemora yet, and the assistant will say so
rather than pretend to check them.
