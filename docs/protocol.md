# USB protocol AD3

Agent Deck uses the Flipper's second CDC interface (interface 1). The normal CLI
stays on interface 0. On this development device they enumerate as COM4 and COM3;
port numbers vary between computers. Exiting the app restores the previous
single-CDC USB configuration.

All messages are printable ASCII with tab-separated fields and a trailing LF.
Lines are shorter than 192 bytes, including the LF. Oversized or NUL-containing
lines are discarded through their newline. Human text cannot contain tabs or
newlines. Non-ASCII names are transliterated where possible.

## Handshake

PC: `PING\tAD3\n`

Device: `HELLO\tAD3\n`

The bridge verifies this handshake before transmitting state. `auto` probes only
Flipper USB VID/PID 0483:5740 ports, never Bluetooth serial ports.

## Optional YOLO setting

On each new connection, the PC disarms the saved Flipper setting:

```text
DISARM\tYOLO\n
SETTING\tYOLO\t0|1\n
```

The Flipper sends `SETTING\tYOLO\t0|1\n` after a disarm and immediately when
the setting changes. A USB/BLE disconnect clears and saves YOLO as off; a
reconnection never silently re-arms it. These lines do not alter AD3 snapshots
or action records.

## Atomic snapshots

```text
BEGIN <connection> <revision> <count>
ROW <id> <label> <state> <event> <reason> <detail> <can_focus>
CTRL <id> <actions> <control_token>
META <id> <field> <offset> <total> <chunk>
... count rows ...
END <revision>
```

Spaces above represent tabs. Bounds:

| Field | Representation |
| --- | --- |
| connection | Eight lowercase hexadecimal characters, regenerated on reconnect |
| revision | Positive uint32, increasing within a connection |
| count | 0–16 |
| id | 1–16 ASCII letters, digits, underscores or hyphens; unique per snapshot |
| label | 1–48 printable ASCII characters |
| state | idle, running, waiting, done, error, offline |
| event | Eight lowercase hexadecimal characters, or `-` for no pending event |
| reason | 1–30 printable ASCII characters; `-` means empty |
| detail | 1–48 printable ASCII characters; `-` means empty |
| can_focus | 0 or 1 |

Each ROW may be followed by META fragments for `session_id` (up to 128 characters)
and `project` (up to 256), plus `review` (up to 512). Fragments are at most 96 characters, start at offset 0,
append contiguously, and repeat the same total length. END rejects incomplete
metadata. META must refer to an already received row. Missing metadata is allowed
for older journal entries and shown as not reported. Project paths longer than
256 display characters end in `...`; the PC retains the path reported by the hook
(up to 4096 characters). Metadata uses the same printable ASCII rules as ROW.

Each row requires one CTRL record. Its bit mask is focus=1, stop=2, continue=4,
approve=8, reject=16; unknown bits, duplicate records, and missing control tokens
for mutating actions are rejected. `control_token` is eight lowercase hexadecimal
characters, or `-` when only focusing or no actions are possible. Review metadata
is a display preview, limited to 512 characters. Longer previews begin with
`Preview truncated` and remain approvable. The bridge retains the original
pending request and sends the decision for its exact request ID; shortening the
display never shortens or changes the approved command or patch. Metadata and
control capabilities commit atomically at END.

Non-ASCII review text uses the normal device display conversion: transliteration
where possible and `?` for unsupported characters. This conversion does not
disable approval or modify the original pending request.

AD3 adds session controls. Update the companion and Flipper app together;
AD1, AD2, and AD3 do not handshake. The normal CLI serial interface remains available
for installing the matching app.

The UI changes only after a valid END matching a complete transaction. Invalid
counts, fields, duplicate IDs, stale revisions, and incomplete snapshots are
rejected. Selection follows the selected agent ID when rows disappear. A
three-second heartbeat timeout displays OFF and disables terminal focusing.

Pending event tokens are stable across heartbeat frames, duplicate hook reports,
and event-journal replay. A newly observed token produces one brief vibration
when enabled. Events arriving in the same snapshot are combined into one pulse.
Reconnecting while the same app instance retains the same pending events does
not buzz again. Reopening the app may notify again about a still-pending request.

Device acknowledgement:

```text
ACK <connection> <revision> <count> <vibration> <alert_count> <buzz_count> <selected_id>
```

The counters are diagnostic counts for the current app run. `buzz_count` counts
vibration requests; the Flipper's global notification settings still apply.

## Button actions

```text
ACT <connection> <sequence> <agent_id> <verb> <control_token>
```

The PC rejects stale connection IDs, duplicate sequence numbers, unsupported
actions, stale control tokens, and actions that are unavailable for the current
thread/turn/request. Verbs are `focus`, `stop`, `continue`, `approve`, and `reject`.
One action runs at a time. Device confirmation retains the original agent,
connection, and control token; a changed snapshot cancels that confirmation.
USB strings are never executed as shell code. Only the fixed Continue preset is
sent as a prompt; approval decisions use exact server request IDs and only
`accept`, `decline`, or `cancel` when declining is unavailable. Cancellation stops
the current turn and is explained on the review screen. No persistent policy amendments or terminal approval keys
are sent. Session states and notification tokens still come from lifecycle hooks.

Agent-action replies are `RESULT\t<sequence>\tOK|ERROR\t<verb>\n`. The device
matches the outstanding sequence; late replies cannot complete a newer action.
Focus retains `RESULT\tOK|ERROR\n`, and initial validation errors can use that
short form. Requests time out visibly rather than being automatically retried.
Approvals report success only after `serverRequest/resolved` (or completion of
that item/turn). A mapped focus verifies foreground activation; an unmapped
shared session launches a CLI view by exact session UUID.
