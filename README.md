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
live runs. **Run Live** and **Schedule Batch** validate and confirm a complete
batch snapshot before it can make changes. Per-device
progress never displays literal commands. Reports default to
`~/Documents/SSH Batch Update/outputs`.
Double-click a file path in the Output column to open that device's output.
Double-click the Result column to view the complete status or error message.
Failed-device output files include the sanitized exception message, with known
connection secrets redacted.

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
earlier ready work. Use **Schedule Batch** to select one future local date and
time. Future jobs do not block ready jobs, and batches scheduled for the same
time run in the visible queue order.

The **Queue** tab shows order, device and command counts, schedule, status, and
result. Pending jobs can be moved, edited, rescheduled, retried, or removed.
Cancelled and completed history entries can also be removed; removing an entry
never deletes its transcripts, summary, or output directory. **Clear Completed**
removes all successful, partially failed, failed, and cancelled history entries
at once while leaving running, queued, and scheduled batches untouched.
Editing loads the encrypted snapshot into the normal form and requires fresh
validation and confirmation before replacing it. Double-click a queue row to
open its per-device results. **Pause Queue** stops new batches from starting;
**Stop Run** safely cancels the active batch at the next checkpoint and also
pauses the queue. A batch with device failures does not prevent the next ready
batch from starting.

Queued device addresses, commands, output settings, and credentials are stored
as a complete macOS Keychain payload. Ordinary application settings contain
only an opaque job ID, ordering, schedule, device type, and counts. The Keychain
payload is deleted when the batch completes, is cancelled, or is removed. A
profile is snapshotted when submitted, so later changes to its `.env` do not
change the queued batch.

Queued work is restored after reopening, but the queue starts paused. An
overdue schedule becomes **Ready** and requires **Resume Queue**. A batch that
was running when the app stopped becomes **Interrupted** and must be retried
manually because some commands may already have run. The app prevents idle
system sleep while an unpaused schedule is armed or a run is active, but it
cannot run while closed or while the Mac is shut down, manually asleep, or has
its lid closed. Keep the Mac logged in, powered, awake, connected to the switch
network, and leave the application open for unattended work.
Verify queue ordering, cancellation, and scheduled live changes against approved
lab switches before using the feature on production access switches.

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
with remaining devices. It does not roll back partial configurations. While it
runs, the console shows the current device, section, source line, and command.
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
bundle never includes `.env`, inventory, command, transcript, or output files.
