# herdr-queue
Read SPEC.md first; it is the source of truth. Hard constraints: no network listeners, read-only (never send input to panes), no new vendors, stdlib-only Python by default.
Test only in an isolated herdr session (`herdr --session herdr-queue-dev`), never `default` or `alice-agents`.
Verify herdr/Claude Code behavior against live docs and the installed binary (`herdr --skill`, `herdr api schema --json`); do not trust remembered CLI shapes.
Start with milestone M0, then proceed in order. Commit per milestone.
