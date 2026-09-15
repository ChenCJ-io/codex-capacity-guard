---
name: capacity-guard
description: Enable, disable, configure or inspect automatic recovery of local Codex conversations after temporary model capacity errors. Always retain the original model and wait without a retry-count limit.
---

# Capacity Guard

Use the Python CLI bundled with this plugin. Resolve `../../scripts/capacity_guard.py`
relative to this skill's directory; do not assume a particular installation path.

- Enable on the user's request: `python3 <script> enable`.
- Enable for one conversation: `python3 <script> enable --thread <UUID>`.
- Disable: `python3 <script> disable`.
- Inspect: `python3 <script> status --json`.
- Diagnose without sending a message: `python3 <script> doctor --thread <UUID>`.
- Set waiting intervals: `python3 <script> configure --initial-delay 30 --max-delay 120`.

The watcher runs independently of model calls. It reacts only to new, genuine
capacity errors in the local Codex runtime log, then continues the same conversation.
For ordinary Codex CLI sessions the default `auto` backend uses `codex queue`,
which talks to the existing local app-server and preserves the session's model.
Use `configure --backend desktop` only for the desktop app's private IPC adapter.
It waits indefinitely using increasing intervals and jitter. Never switch models,
providers, accounts, reasoning settings or permissions to get around an error.

Installation alone does not enable recovery. Enabling starts a local background
process; disabling cancels pending retries. Hooks cancel stale retries when the user
continues or interrupts and restart an already-enabled watcher on subsequent activity.

Desktop compatibility uses a version-dependent local IPC interface. Report `doctor`
failures accurately. Do not create another app-server to take over an open desktop
conversation, edit Codex's internal databases, or send repeated messages after an
ambiguous timeout. Never print credentials, full transcripts or log bodies.
