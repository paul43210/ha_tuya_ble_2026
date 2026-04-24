# CTL20H Fresh-Pair HCI Capture Protocol

Goal: capture the BLE traffic during a factory-fresh pairing of lock #2
with Smart Life so we can decode the **user-registration subcommand**.
Output of this protocol is a single `btsnoop_hci.log` that, once decrypted,
reveals how Smart Life assigns the 8-digit BLE user_id during first pair.

Once decoded, the ha_tuya_ble_2026 fork will be able to register its own
arbitrary user_id at pair time and stop hardcoding `84042128`.

---

## Prerequisites

### On the phone (Android)

1. **Developer Options enabled**
   Settings → About phone → tap Build number 7 times.

2. **USB debugging ON**
   Settings → Developer options → USB debugging (we need ADB to pull the log).

3. **Bluetooth HCI snoop log ON**
   Settings → Developer options → "Enable Bluetooth HCI snoop log"
   (exact label varies: may say "Bluetooth HCI snoop logging" or
   "Bluetooth HCI snoop log buffer").

4. **Restart Bluetooth** — this is NOT optional. Toggling the setting
   doesn't retroactively start logging; only sessions opened after the
   toggle are captured.
   - Turn Bluetooth OFF in settings.
   - Toggle airplane mode on and off (forces a full BT stack restart).
   - Turn Bluetooth ON.

5. **Verify logging is active** before pairing.
   Easiest way: `adb shell ls -l /data/misc/bluetooth/logs/btsnoop_hci.log`
   should show a non-empty file with a recent mtime. If you can't run adb,
   trust the toggle and the BT restart.

6. **Remove the lock from Smart Life and from Android Bluetooth bonds**
   if it was ever there:
   - Smart Life: open device → settings → Remove device.
   - Android BT: Settings → Bluetooth → find "CTL20H SmartLock..." →
     Forget device.

7. **Disconnect other BLE devices** (earbuds, smartwatch, fitness trackers)
   for the duration of the capture. The btsnoop buffer is small and shared
   across ALL BLE traffic; unrelated devices shorten the window for our
   events and muddy the capture.

8. **Confirm account** — the Smart Life account used must be `paul@faure.ca`,
   which is linked to the Tuya IoT project we're using
   (`ywna4snhxyqy3yyegd3c`). This is how I'll retrieve the new lock's
   `local_key` to decrypt the capture.

### On the lock

1. **Factory reset lock #2** per the procedure:
   long-press the setting button through the two-beep, continue through
   the five-beep run, release, wait for the long beep, then a second long
   beep. Lock is now in fresh state.

2. **Put the lock in pairing mode** (it should already be after reset,
   broadcasting as "CTL20H SmartLock XXXXXX"). If the lock stopped
   broadcasting, swipe any registered card OR press the setting button
   once to wake it and re-enter pairing mode.

---

## Capture procedure

Do these steps in tight sequence — minutes, not hours. Btsnoop is a
ring buffer; long idle periods with other BT traffic will evict our events.

1. **Start a timer / note the clock time** (for finding our events later).

2. **Open Smart Life.**
3. **Add Device** → let it scan → select the CTL20H when it appears.
4. **Complete the pairing flow** all the way through:
   - Device name (any).
   - Home assignment ("My Home ..").
   - Any post-pair prompts (skip anything optional).
5. **Wait for pairing to fully complete** — device appears in Smart Life
   device list and shows online.

6. **Perform exactly one unlock** from the Smart Life UI:
   - Open the lock's device page.
   - Tap the unlock button.
   - Confirm lock beeps + opens.

7. **Close Smart Life fully** (swipe away from recents). This ensures no
   further BLE chatter before we pull the log.

8. **IMMEDIATELY pull the capture** — don't wait. Run this on a
   computer that has the phone connected via USB:

   ```
   adb bugreport fresh_pair_bugreport.zip
   ```

   This packages the HCI log plus Bluetooth stack state. Works on
   non-rooted phones.

   OR if you have root / a phone that allows it:
   ```
   adb pull /data/misc/bluetooth/logs/btsnoop_hci.log
   ```

---

## What to send back

1. **The bugreport zip OR the btsnoop_hci.log directly.**
   Drop it into `/home/paul/captures/` on faure.ca or attach to the chat.

2. **The new lock's Tuya device ID** — I'll need this to fetch the
   `local_key` for decryption. You can find it in:
   - Tuya IoT Platform → Devices → list (the ID is the long alphanumeric
     string, like `ebaca64cdnd47sok` for lock #1).
   - OR: Smart Life → device → settings → Device Information →
     "Virtual ID" (sometimes hidden; long-press the device icon).

3. **Approximate timestamps** (if you noted them) for:
   - Start of pairing attempt.
   - Pairing completed (Smart Life confirmed online).
   - First unlock button press.

   Not strictly required — I can find the events by decrypting and
   looking for DEVICE_INFO/PAIR opcodes — but timestamps speed up
   alignment if the buffer has a lot of noise.

---

## What I'll do on receipt

1. Fetch the new lock's `local_key` from Tuya cloud using the device ID.
2. Run `/home/claude/parse_btsnoop.py` to extract ATT writes and
   notifications on the CTL20H MAC.
3. Derive `session_key` from the DEVICE_INFO response's `srand`.
4. Decrypt with `/home/claude/decrypt_v3.py`.
5. Walk the decrypted frames in order:
   - DEVICE_INFO handshake
   - PAIR command
   - **Any subcmd we haven't seen before** — this should contain the
     user-registration payload.
   - Subcmd 0x45 auth (if it appears at all — unclear whether fresh pair
     uses it).
   - Subcmd 0x47 unlock (the one unlock action).
6. Cross-reference the new lock's fresh user_id (visible in
   `ble_unlock_check` DP after the unlock) with the bytes sent in the
   registration subcmd. This tells us whether:
   - Smart Life asks the lock for an ID and the lock assigns one
     (server-side-on-lock), OR
   - Smart Life generates the ID client-side and tells the lock, OR
   - The ID is derived from some Tuya account + device combination.

---

## Likely outcomes

Based on what we know, the most likely shape of the fresh-pair
registration is:

- A new subcmd (probably `0x41`, `0x42`, or `0x4X` we haven't seen)
  sent after PAIR completes, payload containing the proposed user_id
  plus admin flag.
- The lock accepts, binds the user_id to `lock_user_id=1`, and reports
  success.
- Subsequent unlocks use subcmd `0x47` with that user_id, exactly as
  we already know.

If that's the shape, the fork's implementation becomes:
- During HA config flow: generate an 8-digit user_id that's distinctive
  for HA (e.g., `90000001` or `f"{random in some range}"`).
- On first connect after pair: send the registration subcmd with that
  user_id.
- Persist the user_id in the integration config.
- Use it for all subsequent unlocks.

HA now has a dedicated identity independent of Smart Life. You could
factory-reset the lock, re-pair with Smart Life for the initial bind,
then run HA config flow to add HA as a second user — no manual
user_id extraction required, no shared identity with you.

---

## Failure modes to watch for

- **HCI log is empty or doesn't contain the CTL20H MAC.** Snoop toggle
  didn't take; redo BT restart, verify log file grows during a normal
  BLE action (e.g., connecting headphones for 5 seconds).

- **Capture cuts off mid-pair.** Buffer rolled over. Redo with other BLE
  devices disconnected.

- **Smart Life pair fails** (can't find lock, times out). Factory reset
  may not have completed fully — repeat the long-press-through-beeps
  sequence, and verify the lock is advertising (BLE scanner app can
  confirm).

- **Lock pairs but doesn't appear in Tuya IoT device list.** Your IoT
  project is linked to paul@faure.ca and pulls devices from that
  account's homes; sometimes sync takes a minute. Refresh the IoT
  device list after a pause.

- **"Invalid invitation code" style errors.** Doesn't apply to new
  pairing — only to home sharing. Ignore.

---

## Safety notes

- Lock #1 (production) is untouched throughout this procedure. All
  operations are on lock #2.
- Worst case on lock #2 is you factory-reset it again and re-pair; no
  permanent state is at risk.
- Smart Life capture contains BLE session keys for any device active
  during the window. Don't share the raw capture publicly — only with
  me for this analysis.
