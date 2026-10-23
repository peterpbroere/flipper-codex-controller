# Agent instructions

These instructions apply to this entire repository.

## User involvement in design

- Involve the user in every important design decision before implementing it.
  Explain the proposed behavior, meaningful alternatives, and tradeoffs, and
  obtain the user's direction. Do not silently make consequential product
  choices on the user's behalf.
- Important decisions include approval availability, restrictions or capability
  limits, agent/session lifecycle and cleanup, control semantics, substantial UI
  flows, notification behavior, transport behavior, and data retention.
- A broad request to implement a feature or take the next logical step is not
  approval to introduce an additional restriction. README documentation and an
  entry in the decision audit are not substitutes for user agreement.
- Do not repeatedly ask about a decision the user has already made. Proceed with
  routine implementation, refactoring, and fixes that preserve agreed behavior.
- Explain disagreements honestly and with evidence. The user explicitly wants
  independent judgment, not automatic agreement. Verify the actual code and
  observed behavior before explaining a limitation; distinguish our software
  choices from genuine firmware, API, or platform constraints.
- Keep `docs/design-decisions.md` accurate when the user resolves a listed
  decision. Do not treat the other entries as implicitly approved.

## Agreed product requirements

- Keep the native Flipper app in C and the PC companion/bridge in Python.
- Provide an overview of participating agent sessions. Preserve their reported
  states. Do not add inactivity-based OFF states, expiration, or cleanup rules
  without discussing them with the user; inactivity alone is not evidence that
  a session has closed.
- Supported approvals must remain available on the Flipper and PC companion
  regardless of preview length or non-ASCII text. Display limits are presentation
  limits, not permission restrictions. Shorten long previews with a visible
  truncation marker and use the existing device text conversion when needed.
  Keep the original pending request and its scope intact and approve its exact
  request ID. Preserve stale-request, duplicate-action, and connection checks.
- Confirmation uses a normal button press. Do not require a hold to approve or
  confirm an agent control.
- YOLO is an opt-in Flipper Settings toggle beside Vibration and Connection,
  saved on the device and off by default. When enabled, it may accept supported
  command/file-change requests once and answer blocking structured questions
  only when every question has exactly one recommended option. Send Codex the
  exact recommended label and preserve exact request IDs. Never infer answers
  for free-text, secret, ambiguous, or unsupported questions. Do not silently
  include session-wide write grants; those remain manual. A USB/BLE disconnect
  disarms YOLO and saves it off; reconnecting must not silently re-arm it.
- Vibration must be possible to turn off. Preserve the user's saved setting;
  do not silently re-enable it during tests, reconnects, or updates.
- USB and Bluetooth must carry the same supported session notifications and
  agent controls. Discuss any proposed user-facing capability differences.
  Preserve the selected transport, pairing, and settings during routine work,
  and restore the live bridge after device tests that temporarily replace it.
- Document what users actually need for session discovery and control, including
  hooks, trust/session-loading requirements, first-prompt discovery, supported
  host/session types, installation, and connection setup. Do not suggest that
  every open window or internal subagent can be discovered when that is not true.
- Provide packaged Windows releases in both installer and portable ZIP formats;
  build the per-user installer first. Bundle Python and Tkinter so end users do
  not need a source checkout or Python environment. Keep Codex hook registration
  explicit, preserve and back up the user's hook configuration, and offer to
  remove only Agent Deck entries during uninstall. Do not add login autostart.
  Keep the Flipper `.fap` as a separate device install artifact.
- Also provide an x86-64 Linux package as an experimental Ubuntu 22.04-built
  tarball. Clearly state that the packaged app and real-device Linux behavior
  have not yet been verified on a Linux installation.

## Verification and delivery

- Test relevant behavior, including long and non-ASCII approval previews. Check
  that display conversion/truncation never changes the request being approved.
- Distinguish automated tests from real-device validation. State testing gaps
  candidly; do not claim unsupported behavior is impossible or validated.
- Commit and push when requested. A task-specific commit/push request is not
  standing authorization to publish unrelated future changes.
