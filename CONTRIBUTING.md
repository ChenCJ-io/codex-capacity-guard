# Contributing

Thanks for helping make recovery dependable across Codex versions. Keep changes focused and explain the user-visible behavior and how it was checked.

## Local checks

Use Python 3.11 or newer. The runtime has no third-party dependencies.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Default tests must remain offline and use temporary directories and synthetic fixtures. Do not access contributors' real Codex databases or contact a model provider in a unit test. A manual integration test must be clearly named, opt-in, and isolated from real conversations.

## Behavior to preserve

- Recovery keeps the original conversation and model; it never selects a fallback.
- Only genuine runtime capacity errors trigger retries. Quoted text is not a signal.
- Installing does not activate monitoring. Disabling prevents further recovery submissions.
- Manual continuation or cancellation takes precedence over a pending retry.
- A send timeout may mean the request was accepted. Check existing state before sending again.
- A disconnected desktop or unavailable socket must not cause a new server to take over the conversation.
- Logs, diagnostics, fixtures, and committed files must not contain real prompts, tool arguments, credentials, or full conversation exports.

Add focused tests for changes to detection, retry scheduling, cancellation, and ambiguous transport outcomes. Avoid tests that merely repeat an implementation detail.

## Reporting compatibility issues

Include the operating system, Python version, Codex version, selected backend, redacted `doctor`/`status` output, and the observed versus expected behavior. Desktop IPC and the local SQLite schema are version-sensitive, so exact version information matters.

Do not attach your Codex log database, authentication files, state database, or real conversation history. Reduce a failing format to a synthetic fixture with invented UUIDs and text. Inspect command output for usernames, paths, conversation IDs, and any other private details before posting it.

Do not publish exploitable security details in a public issue. Contact a repository maintainer privately first when a private reporting channel is available.

## Pull requests

Use a small, reviewable diff. Explain the behavior change, the compatibility assumptions, and the checks actually run. Update both README languages when commands, configuration, or compatibility limits change.

By submitting a contribution, you agree that it is licensed under this repository's MIT license.
