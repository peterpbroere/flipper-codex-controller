Agent Deck for Linux - EXPERIMENTAL

This x86-64 package is built on Ubuntu 22.04. It has not been verified on a
Linux installation or with a Flipper connected to Linux. The package may need
newer system libraries than those available on older distributions.

Run the desktop companion from this directory:

  ./AgentDeckCompanion

The CLI is ./AgentDeck. To preview Codex hook registration, run:

  ./AgentDeck install-hooks

Review the output, then add --apply to save the hooks. Review and trust them in
Codex with /hooks. Hook registration is not automatic.

USB access may require permission to open the Flipper's serial device.
Bluetooth setup and real-device Linux behavior are unverified. Open on PC,
which opens or focuses a Codex terminal, is currently Windows-only.
