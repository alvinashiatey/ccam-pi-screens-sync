# MPV Multi-Screen Sync

A reusable Raspberry Pi video-wall project. Every Pi runs the same code and reads
the same INI configuration. Its service username or hostname determines whether
it is the master or a client.

## What this replaces

- separate, hand-edited `master.sh` and `client.sh` files
- video paths embedded in systemd services
- sync processes that occupy an SSH terminal

The installer creates one background service named `video-sync`. The existing
fullscreen mpv process is controlled through `/tmp/mpv-sync.sock`.

## Before installing

On every Pi, mpv runs as the same user that runs video-sync and includes this
option:

```text
--input-ipc-server=/tmp/mpv-sync.sock
```

The video file should exist at the configured path on every Pi. For best results,
use identical copies of the same encoded file and keep the Pi clocks synchronized
with NTP/chrony.

## First project: kiosk9 as master

The included [`config.ini`](config.ini) is ready for kiosk9 at `10.74.0.187` as
the master. The ten client addresses are configured from `10.74.0.179` through
`10.74.0.189`, excluding the master's address.

| Device | Address | Role |
| --- | --- | --- |
| kiosk | 10.74.0.179 | Client |
| kiosk2 | 10.74.0.180 | Client |
| kiosk3 | 10.74.0.181 | Client |
| kiosk4 | 10.74.0.182 | Client |
| kiosk5 | 10.74.0.183 | Client |
| kiosk6 | 10.74.0.184 | Client |
| kiosk7 | 10.74.0.185 | Client |
| kiosk8 | 10.74.0.186 | Client |
| kiosk9 | 10.74.0.187 | Master |
| kiosk10 | 10.74.0.188 | Client |
| kiosk11 | 10.74.0.189 | Client |

## Install on each Pi

Clone the repository into the user's home directory, edit `config.ini`, then run:

```bash
cd ~/pi-sync
sudo ./install.sh
```

Run that same command on all Pis. The installer detects the current user and
hostname and installs the service. Since these Pis all use the hostname `ccam`,
their distinct account names (`kiosk`, `kiosk2`, etc.) identify them. It also
checks for Python 3 and mpv. When
either is missing on Raspberry Pi OS, Debian, or Ubuntu, the installer updates
the package information and installs it automatically. An internet connection is
needed only when packages must be installed.

### Existing ffplay login profiles

The installer checks the kiosk user's `.bash_profile`. If it finds an `ffplay`
command, it makes a one-time backup named `.bash_profile.before-pi-sync` and
replaces only that command with the installed `mpv-kiosk` launcher. The existing
VT1 check, video lookup, comments, and unrelated profile settings are preserved.

If no player command exists, the installer appends a small managed VT1 block. If
mpv is already configured with `/tmp/mpv-sync.sock`, it makes no profile change.
The shared launcher selects the first `.mp4` or `.mov` file under the current
user's `~/mov` folder, so it works for `kiosk`, `kiosk2`, and every other account
without embedding usernames in the project.

The installer also installs and configures Chrony by default. On `kiosk9`, Chrony
serves time to the configured local network. On every other Pi, Chrony uses
`kiosk9.local` as its preferred time source. These settings come from the
`[clock]` section of `config.ini`, so they can be reused on another network.

The default clock configuration is:

```ini
[clock]
manage_chrony = yes
master_address = 10.74.0.187
allow_network = 10.74.0.0/24
```

Change `allow_network` if the Pis are not on `10.74.0.x`. If `.local` names do
not resolve reliably, use fixed IP addresses as this configuration does.

Check clock synchronization on the master and clients with:

```bash
timedatectl status
chronyc tracking
chronyc sources -v
```

Or use the project helper:

```bash
syncctl clock
```

On clients, `chronyc sources -v` should list kiosk9 and normally mark it as the
selected source. The master continues using the upstream time sources supplied
by Raspberry Pi OS and can keep serving a common local clock during a temporary
internet outage. UDP port `123` must be allowed from the clients to kiosk9 if a
firewall is enabled.

Useful commands:

```bash
syncctl status
syncctl logs
syncctl restart
syncctl stop
syncctl start
syncctl clock
syncctl set-video /home/kiosk9/mov/my-video.mp4
syncctl local-video /home/kiosk10/mov/standalone-video.mp4
syncctl resume-sync
syncctl config
```

`set-video` saves the chosen path outside Git, so pulling a code update does not
change the active video. Run it on each Pi if their absolute video paths differ.
If all Pis use the same path, the `default_video` value in `config.ini` is enough.

### Play without synchronization

To stop synchronization on one Pi and play a local looping video there:

```bash
syncctl local-video /home/kiosk10/mov/standalone-video.mp4
```

This affects only the Pi where the command is run. It stops `video-sync`, loads
the selected file into the existing fullscreen mpv player, resets playback speed
to normal, and starts playing immediately. The other Pis continue unchanged.

To make that Pi rejoin the synchronized installation:

```bash
syncctl resume-sync
```

The Pi loads its saved synchronized video and, as a client, joins the current
master session when it receives kiosk9's packets.

## Updating later

Pull the new version and reinstall it on each Pi:

```bash
cd ~/pi-sync
git pull --ff-only
sudo ./install.sh
```

The active video selection is preserved.

## Starting a new installation/project

Copy `config.ini`, then change only:

- `master_device`
- `targets`
- `default_video`
- `clock.master_address`
- `clock.allow_network`
- timing values, if testing shows they need adjustment

Do not put site-specific IP addresses or video paths into the program itself.

## Web dashboard

The installer runs a small authenticated control agent on every Pi. On kiosk9 it
also starts the dashboard. Open it from another computer on the display network:

```text
http://10.74.0.187:8080
```

Before installing, create a private `.env` file. It is ignored by Git. Generate
one shared random token and copy the same `.env` file to every Pi:

```bash
cp .env.example .env
openssl rand -hex 32
```

Place the generated value in `.env`:

```dotenv
PI_SYNC_API_TOKEN=paste-the-generated-value-here
```

The public web settings remain in `config.ini`:

```ini
[web]
agent_port = 5010
dashboard_port = 8080
upload_limit_gb = 20
```

During installation, `.env` is copied to `/etc/video-sync/pi-sync.env` with
permissions `0600`, readable only by root. The installer refuses to continue if
`.env` is missing or its token is shorter than 24 characters. Never commit or
send this token through an insecure public channel.

Because `.env` is intentionally absent from Git, copy it separately into the
`~/pi-sync` directory on every Pi before running `sudo ./install.sh`.

The dashboard can:

- show online/offline, playback mode, disk space, clock state, and measured drift
- upload a video to kiosk9 and copy checksum-verified versions to all clients
- start synchronized playback on selected screens (kiosk9 is included automatically)
- play a video locally on selected screens
- stop selected screens

Uploads are first written to a temporary file and moved into place only after the
transfer completes. Distribution uses SHA-256 verification. A partial or corrupt
copy is never promoted to the media library.

This dashboard is intended for the trusted local display network. It uses a
shared token but plain HTTP, so do not expose ports `5010` or `8080` to the public
internet. If a firewall is enabled, allow TCP `5010` between kiosk9 and the Pis,
TCP `8080` from operator computers to kiosk9, UDP `49999` for video sync, and UDP
`123` for Chrony.

## Troubleshooting

```bash
hostname
whoami
ls -l /tmp/mpv-sync.sock
systemctl status video-sync
journalctl -u video-sync -n 100 --no-pager
```

Hostnames are compared case-insensitively. UDP port `49999` must be allowed from
the master to every client. If a hostname does not resolve, use that Pi's IP in
`targets`.
