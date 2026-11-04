# SSH batch command runner

Run an ordered text command file across Cisco or Huawei devices through SSH using
[Netmiko](https://github.com/ktbyers/netmiko). The program starts in dry-run mode;
it does not connect to a switch unless `--apply` is present.

## Setup

```sh
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Set your SSH login in `.env`. The SSH password and optional enable secret never
need to be placed in a command file. Keep `.env` private; it is gitignored.

For the macOS desktop interface, install the GUI dependencies instead:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-gui.txt
python gui.py
```

The GUI provides editable devices, exec-command, and config-command text
boxes. Devices can be imported from or exported to a plain text file, and
**Clear Devices** empties the device input. One shared command import loads an
existing sectioned `[exec]`/`[config]` file into both command boxes, **Export
Commands** saves both boxes back to that same compatible format, and **Clear
Commands** empties both boxes. Exec commands always run first, followed by
config commands.
The username and masked password can be entered directly or loaded from a saved
credential profile. Profiles reference user-managed `.env` files and the last
selected valid profile reloads when the app reopens. Only the friendly profile
name and file path are saved in GUI settings; credential values are never
copied there. **Validate Input** performs no SSH connections. **Test
Connections** logs in to each valid IP, detects the prompt, optionally verifies
enable mode, and disconnects without running either command box. Its status,
stage, full error, and diagnostic file appear in the same results table used by
live runs. **Run Live** and **Schedule Run** validate and confirm a complete
batch snapshot before it can make changes. The **Device Results** progress
table does not display literal commands. Reports default to
`~/Documents/SSH Batch Update/outputs`.
Double-click a file path in the Output column to open that device's output.
Double-click the Result column to view the complete status or error message.
Failed-device output files include the sanitized exception message, with known
connection secrets redacted. Failed results also show an operational category,
such as Connectivity, Authentication, Privilege, Command Rejected, or Command
Timeout. The category summarizes the failure; the sanitized result and
transcript remain the detailed diagnostic record.

### Credential profiles

Use **Manage Profiles…** to add, rename, relink, or remove credential profiles.
Adding or relinking selects an existing `.env` containing:

```text
SSH_USERNAME=admin
SSH_PASSWORD=replace-me
ENABLE_SECRET=optional-enable-secret
SSH_PORT=22
SSH_TIMEOUT=15
```

Only `SSH_USERNAME` and `SSH_PASSWORD` are required. Removing a profile removes
the app's reference but never deletes or edits its `.env` file. Editing a loaded
username or password switches the GUI to Manual mode for that run. Because
`.env` passwords are plaintext, keep those files private, restrict their file
permissions, and never commit them to Git.

### Batch queue and scheduling

The GUI runs only one SSH batch at a time. **Run Live** always appends the batch
to the end of the queue. It starts automatically when nothing else is running,
or waits in FIFO order when another batch is active; it never jumps ahead of
earlier ready work. Use **Schedule Run** to select a future local date and time.
Use **Settings → Preferences…** in the macOS menu bar to choose the theme,
set the scheduled batch timeout, and choose what happens after a device fails.
Hover over the **?** next to the timeout for its explanation.
The timeout is 30 minutes, 1 hour, or 2 hours (1 hour by default). It starts
at the scheduled time and is saved with each new or rescheduled batch. A batch
may begin at its expiry time, but it becomes **Expired** if it has not started after
that time. Changing the setting does not change already queued deadlines, and
expiry never stops work that is already running. Future jobs do not block ready
jobs, and batches scheduled for the same time run in visible queue order.

By default, a failed device does not stop the other devices in its batch.
Choose **Stop after first failure** to leave all remaining devices unrun; they
appear as **Skipped** in Device Results and `summary.csv`. This choice is saved
with each new batch, so changing Preferences does not affect queued work.
It does not stop later batches in the queue.

The SSH form's **Output files** choice applies to each submitted batch. By
default, each device gets its own transcript. Choose **One file per batch**
to put all attempted device transcripts in one file named after the batch.
Device Results and `summary.csv` point each attempted device to that shared
file. The output choice can be changed when editing a queued batch; connection
tests continue to write separate device diagnostics.

The **Queue** tab shows order, batch name, device and command counts, schedule,
expiry time, status, and result. Pending jobs can be moved, edited,
rescheduled, retried, or removed. Cancelled, completed, and expired Queue
entries can also be removed; removing an entry never deletes its transcripts,
summary, or output directory. **Clear Completed** removes all successful,
partially failed, failed, cancelled, and expired Queue entries at once while
leaving running, queued, and scheduled batches untouched.
Editing loads the encrypted snapshot into the normal form and requires fresh
validation and confirmation before replacing it. Double-click a queue row to
open its per-device results. **Pause Queue** stops new batches from starting;
**Stop Run** safely cancels the active batch at the next checkpoint and also
pauses the queue. A batch with device failures does not prevent the next ready
batch from starting.

The **Live Output** tab follows the active batch regardless of which Queue row
is selected. It shows each literal exec or config command as it is sent and the
switch's response when that command returns. It may reveal passwords or other
sensitive text. The last run stays visible until another starts or the app
closes; connection tests do not appear there. The view keeps roughly the latest
2 MB of text, while saved output files retain the complete transcript. Use the
**Text size** controls above the output box to adjust its font from 50% to 200%;
the percentage is remembered when the app reopens. At 100%, the text matches
the previous 120% size. In **Settings → Preferences…**, choose **By batch** to
keep all devices in the live view (the default), or **By device** to clear the
view when the next device starts. Saved output files are unaffected.

For a completed batch with device failures, **Retry Failed/Skipped Devices…**
creates an editable SSH-form draft containing failed and skipped devices and
the original commands. Successful and cancelled devices are excluded. The app reloads the
original named profile when it still exists; otherwise it clears credentials
and selects Manual. Review and validate the draft before submitting it as a new
batch. No retry is queued or executed automatically.

Queued device addresses, commands, output settings, and credentials are stored
as a complete macOS Keychain payload. Ordinary application settings contain
only an opaque job ID, batch name, ordering, schedule window, timeout setting,
friendly profile label, device type, and counts. The Keychain payload is deleted
when the batch completes, is cancelled, expires, or is removed. A
profile is snapshotted when submitted, so later changes to its `.env` do not
change the queued batch.

Queued work is restored after reopening, but the queue starts paused. A new
schedule restored after its expiry time becomes **Expired** and its
Keychain payload is deleted. Older saved schedules that predate deadlines keep
their legacy no-expiry behavior. A batch that
was running when the app stopped becomes **Interrupted** and must be retried
manually because some commands may already have run. The app prevents idle
system sleep while an unpaused schedule is armed or a run is active, but it
cannot run while closed or while the Mac is shut down, manually asleep, or has
its lid closed. Keep the Mac logged in, powered, awake, connected to the switch
network, and leave the application open for unattended work.
Verify queue ordering, cancellation, and scheduled live changes against approved
lab switches before using the feature on production access switches.

### Audit history dashboard

The full-window **History** screen keeps a persistent audit index for live
batches that run after the feature is installed. It does not import older
output folders and does not record connection tests. The two top-level tabs
are **SSH** and **History**; the app always starts on **SSH**.
History is loaded only when its screen is first opened, so it does not delay
normal queue use or SSH execution.

Queued and scheduled work continues while History is visible. When a batch
starts and its device results begin updating, the **SSH** and **Device Results**
tabs pulse briefly and retain a dot until **SSH** → **Device Results** is opened,
without interrupting History review.

The dashboard defaults to the last 30 days and provides summary cards, a line
chart of successful and failed devices, a donut chart that switches between
run status and failure causes, searchable and filterable records, and 100-row
pagination. Legacy failures without stored categories appear as
**Uncategorized**. Filters include date range, status, manual or scheduled
source, device type, and credential-profile label.
Selecting a record can load its `summary.csv` into **Device Results**, open its
output or summary, or show its encrypted command snapshot after a sensitive
content warning. **Retry Failed/Skipped Devices…** can also create a draft for
failed and skipped devices from a historical summary and encrypted command snapshot; credentials are
reloaded from an existing named profile rather than History.

Audit metadata is stored in an owner-private SQLite database at the app's
macOS Application Support location. The database contains batch metadata,
aggregate result and failure-category counts, friendly profile labels, and
output paths. It never contains usernames, passwords, enable secrets, command
text, device addresses, or per-device error details. Device details are read
from the existing `summary.csv` only when requested, so moved or deleted output
is shown as unavailable rather than copied into another data store.

Exact command snapshots are encrypted separately in macOS Keychain under
service `com.sg4291.sshbatchupdate.history.commands`. They can include literal
password commands, so **View Commands** displays a warning. Queue payloads and
credential snapshots continue to be removed at the end of a run; the
command-only history snapshot remains until its audit record is removed.

**Remove from History** deletes the database reference and command snapshot but
preserves transcripts and reports. **Clear History** does the same for every
record. **Delete History & Outputs** is a separate per-run destructive action
that requires confirmation and only operates on a validated, marked `run_`
directory; there is no bulk output deletion. **Clear Completed** affects only
the current Queue display and never deletes persistent audit history.

History records are kept until manually removed. An unfinished record is
marked **Interrupted** after the next launch and is never retried
automatically. A history or Keychain error is reported in the interface but
does not stop an already confirmed live batch.

### Command dictionary

The reusable, non-modal **Command Dictionary** window is opened from the SSH
command controls and contains 96
entries organized as 48 cross-vendor pairs for Cisco Catalyst 9300L and Huawei
CloudEngine S5755-S. Seventy entries are read-only operational commands. The
remaining entries are explicitly marked as configuration changes, service-
impacting actions, active diagnostics, or interactive saves and show their
required context. Its scope is deliberately exact:

- Catalyst 9300L documented for IOS XE 17.3.x, with deployed target 17.3.3
- CloudEngine S5755-S documented for V600R024C10, with deployed target
  V600R024C10SPC500 / YunShan OS 1.24.1.1

Search uses command text, purpose, aliases, keywords, and related metadata.
Platform and category filters can be combined with it. Selecting a row shows
the exact syntax, purpose, applicability, deployed target, mode, risk, and
notes. A **Copy** action directly below the primary syntax copies that literal
syntax; entries containing an angle-bracket placeholder also show a replacement
warning. Reversible entries show a separate rollback syntax with its own
**Copy** action. Each action copies exactly one command and never inserts it
into the execution form.

Browsing or copying a dictionary entry never inserts it into the command boxes,
executes it, records it in History, persists a selection, or makes a network
request. The dictionary is a release-scoped reference, not proof that a command
is supported or safe on every image, license, hardware revision, role, or site
configuration. Replace every `<PLACEHOLDER>` deliberately, enter the documented
command context first, treat rollback text as a reviewed aid rather than an
automatic restoration of prior state, and validate the result on an approved
lab switch before production use. Save commands may prompt interactively and
must not be treated as unattended operations.

### Config converter

The reusable, non-modal **Config Converter** window opens below the Command
Dictionary control. It converts a conservative command and configuration
baseline between generic Cisco IOS XE and Huawei VRP syntax entirely in memory;
it does not connect to a device, queue work, or create audit History. Source text
can be pasted or imported from `.cfg` and `.txt` files. Select a source vendor or
use auto-detection, then click **Convert** to produce an editable target candidate.

The reviewed baseline includes the Dictionary's operational command pairs plus
hostnames, VLANs, common interface access/trunk settings, descriptions, MTU and
administrative state, SVIs and IPv4 addresses, static routes, link aggregation,
LLDP, DNS, NTP, syslog, and directly compatible basic STP settings. Interface
numbering and command availability still depend on the actual hardware and
software release. High-speed interface names and site-, security-, or
hardware-specific features such as AAA/NAC, QoS, SNMP credentials, PKI, stack/HA,
and non-equivalent STP behavior are left as blocking review items rather than
guessed.

Unsupported input remains visible as a target-native `REVIEW` comment and is
explained in the review table. Passwords, hashes, communities, shared keys, and
cipher values are redacted instead of copied. Editing the result requires
**Recheck** before executable handoff. Plain target text can be exported as a
review candidate even when issues remain, but SSH `[exec]`/`[config]` export and
**Import to SSH Commands** are blocked until every review marker, unknown command,
and placeholder is resolved. Importing a clean result classifies operational and
configuration commands, updates Device Type, and offers Replace or Append when
the SSH command boxes already contain text.

Converted output is a human-review candidate, not a production-ready migration.
Validate it against vendor documentation and an approved lab switch before use.

## Use

Put one IPv4 or IPv6 address per line in an inventory file. Hostnames are not
accepted. Blank lines and `#` comments are ignored. Then create a command file using `[exec]` and
`[config]` headers. Sections may repeat and run in file order:

```text
[exec]
show version

[config]
snmp-agent sys-info version v3
snmp-agent usm-user v3 sea_snmpv3_user authentication-mode sha
the-real-snmp-password
the-real-snmp-password
```

Every nonblank, non-comment line is sent literally. In a `[config]` section,
each line is timing-based so lines following an interactive command can provide
password responses. This means configuration passwords are deliberately stored
in the command file and will appear in transcripts.

Validate files without connecting:

```sh
python3 run_commands.py devices.txt commands.txt --device-type huawei
```

Run commands:

```sh
python3 run_commands.py devices.txt commands.txt --device-type huawei --apply
python3 run_commands.py devices.txt commands.txt --device-type cisco_ios --apply
```

`--device-type` is passed directly to Netmiko, so another supported platform
identifier may be used when needed. Add a site-specific failure message with
repeatable `--failure-pattern` options, for example:

```sh
python3 run_commands.py devices.txt commands.txt --device-type huawei --apply \
  --failure-pattern 'permission denied'
```

Each apply run creates an owner-only timestamped directory under `outputs/`
(or `--output-dir`) containing one exact device transcript and `summary.csv`.
The runner stops only the affected device after a command error and continues
with remaining devices by default. Add `--stop-on-first-failure` to skip later
devices after any device failure. It does not roll back partial configurations.
While it runs, the console shows the current device, section, source line, and command.
Those progress messages include literal command-file passwords, so do not share
or redirect that output where others can read it.

Each GUI connection test creates a separate owner-only
`connection_test_YYYY-MM-DD_HH-MM-SS` directory containing sanitized per-device
diagnostics and `summary.csv`. Credentials, enable secrets, and command-box
contents are not written to connection-test diagnostics.

## Build the internal macOS app

Install the development dependencies and build the native app on a Mac:

```sh
python -m pip install -r requirements-dev.txt
pyinstaller --noconfirm --clean ssh_batch_update.spec
```

The unsigned application is created at `dist/SSH Batch Update.app`. The GitHub
Actions workflow builds separate Intel and Apple Silicon ZIP files and performs
unit, GUI, packaging, and startup checks.

For internal installation:

1. Download the ZIP matching the Mac (`x86_64` for Intel or `arm64` for Apple Silicon).
2. Extract it and move `SSH Batch Update.app` into Applications.
3. On first launch, use macOS's explicit Open or Open Anyway flow when prompted.
4. Enter or import the devices and commands, then enter credentials manually or add a local `.env` profile.

If `.env` is hidden in the macOS file picker, press Command-Shift-Period to show
hidden files.

The app is intentionally unsigned. Gatekeeper warnings are expected, and a
company security policy may prevent unsigned applications from running. The app
bundle never includes `.env`, inventory, command, transcript, output,
audit-database, or Keychain payload files.
