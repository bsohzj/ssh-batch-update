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

## Use

Put one IP address or DNS hostname per line in an inventory file. Blank lines
and `#` comments are ignored. Then create a command file using `[exec]` and
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
with remaining devices. It does not roll back partial configurations.
