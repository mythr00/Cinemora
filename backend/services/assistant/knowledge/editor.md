id: editor
title: The Editor — revisions and renders
link: /projects/[id]/edit
keywords: editor, edit, revision, render, export, trim, operations, change

The Edit tab (/projects/[id]/edit) is where a finished production
is revised without re-running the whole pipeline. Editing is
non-destructive: the production's artefacts are imported read-only
as revision 0, and every batch of edits creates the next revision
in a chain, so earlier versions always remain available.

In the editor you can apply operations to a revision (scene and
asset changes, music and volume adjustments, trims), upload your
own media to the project, and request a render of a revision —
rendering produces a new film from that revision's document while
the original production stays untouched.

Manual edits never make an asset "verified" by fiat: replacements
re-earn their verdict through the same evidence machinery as
pipeline visuals, and uploads are recorded as user-supplied with
unknown rights. Each revision has its own certificate on the
editor endpoints, separate from the original production's
certificate.
