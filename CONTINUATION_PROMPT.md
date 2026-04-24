# CTL20H Lock Integration — Continuation Prompt

*Copy-paste this into a new Claude chat to resume work on the CTL20H lock
integration. The new chat needs memory access enabled. All context below is
pointers into canonical sources — no information is duplicated that already
lives in memory or in committed files.*

---

**I'm continuing work on a custom fork of the Home Assistant `tuya_ble`
integration for a MOES CTL20H cabinet lock. The protocol is fully decoded
and v0.3.0 works end-to-end. Please start by reading the sources below in
order, then wait for my next ask before making changes.**

## 1. Read in this order

**First — your memory.** The CTL20H entries contain hardware identifiers,
BLE UUIDs, cryptography keys (local_key), the registered BLE user_id, and
Tuya IoT portal credentials including the Access Secret (only shown once at
project creation — irreplaceable if lost). Specifically look for memory #13
(hardware/creds) and the separate DP semantics entry.

**Second — the canonical protocol reference.**
- GitHub: https://github.com/paul43210/ha_tuya_ble_2026/blob/main/CTL20H_PROTOCOL_NOTES.md
- On faure.ca: `/home/ha_tuya_ble/CTL20H_PROTOCOL_NOTES.md`
- ~25 KB, 14 sections, covers: wire format, session handshake, V4 DP framing,
  subcmds 0x45/0x47, DP semantics table, battery wrapper quirk, fork layout,
  version history, open follow-ups.

**Everything in this prompt is pointers.** Don't re-derive; consult the
sources.

## 2. Infrastructure

| Thing | Where |
|---|---|
| Home Assistant | `myha.51woodhill.faure.ca` (Beelink Mini S13, HAOS). Use HA-MCP tools. |
| Dev server | `faure.ca`. Use Faure.ca/MCP tools for file ops and bash. |
| Fork clone | `/home/ha_tuya_ble/` on faure.ca |
| Git remote | https://github.com/paul43210/ha_tuya_ble_2026 |
| HA install path | `/config/custom_components/tuya_ble/` (don't edit here directly) |
| Install mechanism | HACS custom repository. Updates land via GitHub Releases. |
| Analysis scripts | `/home/claude/parse_btsnoop.py`, `/home/claude/decrypt_v3.py` |

## 3. Credentials

| Credential | Value | Notes |
|---|---|---|
| GitHub username | `paul43210` | |
| GitHub PAT | *In memory* (under user's CTL20H memory edits) | scope: `repo`, pre-authenticated for `git push` and Releases API. Not stored in this file to avoid triggering GitHub's secret-scanning push protection. |
| Tuya IoT Access Secret | In memory #13 | Back up; only shown once at project creation |
| Lock's BLE user_id | `84042128` | Hardcoded in fork; captured from Smart Life pairing |

## 4. Dev workflow (proven over ~15 releases)

1. Edit under `/home/ha_tuya_ble/` on faure.ca via Faure.ca/MCP.
2. `python3 -m py_compile <edited files>` to catch syntax errors.
3. `git add -A && git commit -m "…" && git push origin main`.
4. Bump `"version"` in `custom_components/tuya_ble/manifest.json`.
5. `git tag -a vX.Y.Z -m "…" && git push origin vX.Y.Z`.
6. `POST https://api.github.com/repos/paul43210/ha_tuya_ble_2026/releases`
   with `tag_name`, `name`, `body` — HACS requires a published Release, not
   just a tag.
7. Paul: HACS → Tuya BLE → Redownload → pick new version → restart HA
   (or reload integration).
8. Paul enables debug logging for `custom_components.tuya_ble` if testing
   anything new, and sends `/config/home-assistant.log` back.

## 5. Debug process

**From the HA log side.** Grep for these to reconstruct what happened:

- `Successfully connected` — BLE link up
- `FUN_RECEIVE_DP_V4 payload (...)` — every inbound data packet
- `jtmspro auth prompt received` — 14-byte greeting detected (v0.2.9+)
- `V4 SUBCMD SEND idx=N subcmd=0xXX payload=...` — outbound subcommand
- `V4 DP SEND idx=N dp_id=X type=Y value=Z` — outbound DP write
- `battery wrapper (dp=X) -> DP 8 battery = Y%` — bulk status decoded
- `Device unexpectedly disconnected` — BLE dropped (check RSSI)
- `V4 DP parse: len ... overflows buffer` — malformed inbound (investigate)

**For new protocol mysteries.** Paul grabs a fresh btsnoop HCI capture from
his Android phone (Developer Options → "Bluetooth HCI snoop log" → reproduce
the action via Smart Life → pull `/data/misc/bluetooth/logs/btsnoop_hci.log`
via ADB/bug report). We decrypt with `/home/claude/decrypt_v3.py`, using
`local_key[:6]` = `E=a[>x` for login_key and session_key derived per-session
from the DEVICE_INFO response's `srand` (bytes 6–12).

## 6. Current state (v0.3.0 at time of writing)

Working: session auth, bulk status, battery %, DP 33 lock toggle (silent
persistent), subcmd 0x47 momentary unlock (beeps, auto-relock 5s), DP 47
motor state (device_class=LOCK), DP 21 alarm ENUM, proper entity naming
("Lock" / "Unlock" / "State (Locked/Unlocked)").

See §11 of `CTL20H_PROTOCOL_NOTES.md` for the full version history.

## 7. Outstanding / open follow-ups

In rough priority order (see §14 of `CTL20H_PROTOCOL_NOTES.md` for detail):

1. **user_id derivation.** `84042128` is hardcoded. Factory reset invalidates
   it. Need to understand how Smart Life generates it (likely hash of Tuya
   account ID or device UUID) so the fork can register fresh IDs via
   subcmd 0x45 after a re-pair. Biggest robustness gap.
2. **Beelink Mini S13 BIOS — "Restore on AC Power Loss".** Manual power
   button currently required after power loss. Non-fork task but real.
3. **`lock.*` entity migration.** Currently switch + button. Could become a
   proper HA lock entity with `lock.lock` / `lock.unlock` / `lock.open`.
   Nice-to-have.
4. **RFID unlock detection.** *Deferred by Paul.* Would require persistent
   BLE connection (battery cost) or state-delta-on-reconnect heuristics;
   also needs fresh HCI capture with integration connected during an RFID
   unlock to decode the source discriminator. Do not pursue unless Paul
   reopens it.
5. **Upstream "first Submit fails" config-flow bug.** Not our code; Paul
   works around by clicking Submit twice.

## 8. Paul's working preferences

- **Confirm approach before making changes**, especially for anything
  touching the lock. This is a safety-critical device housing medications
  for a family member at self-harm risk. Cloud dependency is non-negotiable
  — must remain local-only BLE.
- **Push back on flawed assumptions.** Paul does this well; expect it and
  accept gracefully. Run char-counts, byte-counts, size checks rather than
  eyeballing.
- **Stop on errors, ask for direction** rather than charging ahead through
  failures.
- **Canadian context** (amazon.ca, CAD pricing) where relevant.
- **Mobile-friendly responses** for conversational work; detailed docs in
  artifact files are fine.
- **Prompts and .md files as downloadable artifacts** (write to disk and
  commit, don't just dump in chat).
- **Factual with references**; no guessing.

## 9. What I want you to do first in the new chat

Nothing. Read the memory, read `CTL20H_PROTOCOL_NOTES.md`, acknowledge
you're up to speed, and wait for my next message. I'll tell you which item
(from §7 or new) to pick up.

---

*This prompt is versioned at
`/home/ha_tuya_ble/CONTINUATION_PROMPT.md` on faure.ca and committed to
https://github.com/paul43210/ha_tuya_ble_2026. Update when new learnings
land or v0.3.x+ changes the state-of-play.*
