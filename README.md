# Niko Access Control (2-wire) for Home Assistant

[![hacs](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)

Unofficial Home Assistant integration for the **Niko Access Control 2-wire**
video door system (article **510-31001** door station and friends), controlled
through the Niko / Hik-Connect cloud with **local live video**.

> **Not affiliated.** This is an independent, community-built project. It is
> **not affiliated with, authorized, maintained, sponsored or endorsed by Niko
> Group NV or Hangzhou Hikvision Digital Technology Co., Ltd.** "Niko", the Niko
> logo, "Hik-Connect", "EZVIZ" and "Hikvision" are trademarks of their
> respective owners and are used here only nominatively, to describe the
> hardware this project interoperates with. Use at your own risk. See
> [Legal & trademarks](#legal--trademarks).

## What works

| Feature | Notes |
|---|---|
| 🔔 Doorbell `ring` event | `event.*_doorbell` — trigger automations on a ring |
| 📞 Call state | `idle` / `ringing` / `in_call` sensor + a "ringing" binary sensor |
| 🚪 Open door | one button per enabled lock on the station |
| 📹 **Live video** | pulled **locally** from the station (H.264 → MJPEG); no admin password or RTSP needed |
| 🟢 Connectivity | cloud online/offline diagnostic |

Not included: **two-way talk** (audio) — the media protocol is reverse-engineered
but the cloud call-bridge step isn't reproduced yet.

## Requirements

- A **Niko Access Control** account (the one you use in the Niko app).
- **Home Assistant on the same LAN as the door station** — the video is pulled
  directly from the station over your local network (ports 9010/9020). Cloud-only
  / off-site Home Assistant can still do ring, call state and unlock, but not video.
- `ffmpeg` (bundled with Home Assistant OS/Container).

## Installation

### HACS (recommended)
1. HACS → ⋮ → **Custom repositories** → add this repo, category **Integration**.
2. Install **Niko Access Control (2-wire)**, then restart Home Assistant.

### Manual
Copy `custom_components/niko_access` into your HA `config/custom_components/` and
restart.

## Setup

1. **Settings → Devices & services → Add integration → Niko Access Control.**
2. Log in with your Niko app account (e-mail/phone + password). Only an MD5 of
   the password is stored, because that is what the cloud expects.
3. Open **Configure** on the integration:
   - **Door station IP** — auto-detected on your network (SADP). Leave empty to
     disable video. Set a **DHCP reservation** for the station so it stays put
     (the integration also re-discovers it if the address changes).
   - **Lock IDs** — leave empty to expose the locks the station reports as
     enabled, or list them (e.g. `3` or `1,3`).
   - **Poll interval** and **stream quality** (`main` full-res / `sub` lighter).

You get one device ("External unit") with the doorbell event, call-state and
ringing sensors, the open-door button(s), and a **Live video** camera.

## How it works (short version)

The Niko system is a rebranded Hikvision 2-wire intercom; the app is a re-skinned
Hik-Connect on the `guardingvision` OEM cloud. Ring/unlock/call-state go through
that cloud API. Video is **not** cloud-relayed here: a per-device key is obtained
from the cloud (no device password), then the H.264 stream is pulled directly
from the station over the LAN (the "CPD7" protocol), de-framed in Python and
transcoded to MJPEG.

## Development

```bash
uv venv -p 3.13 .venv
uv pip install pytest-homeassistant-custom-component pycryptodome cryptography \
  xmltodict ha-ffmpeg PyTurboJPEG ruff
uv run pytest        # tests
uv run ruff check custom_components tests
```

A local dev Home Assistant is provided: `docker compose up -d` → http://localhost:8123
(mounts the integration read-only; `docker compose restart` to reload).

## Credits

- Local video (CPD7) builds on the excellent, MIT-licensed
  **[rtammekivi/hikconnect_intercom](https://github.com/rtammekivi/hikconnect_intercom)**
  (vendored under `custom_components/niko_access/cpd7/`). See
  [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md).

## Legal & trademarks

This project is an independent interoperability effort. It contains no Niko or
Hikvision source code; the cloud/device protocols were determined by observing a
device the author owns, for the purpose of interoperability.

"Niko" and the Niko logo are registered trademarks of **Niko Group NV**.
"Hikvision", "Hik-Connect" and "EZVIZ" are trademarks of **Hangzhou Hikvision
Digital Technology Co., Ltd.** These names are used only to identify the
compatible hardware and service; no affiliation or endorsement is implied.

The brand images in `custom_components/niko_access/brand/` (the Niko app icon and
wordmark) are included **only to identify the integration** in the Home Assistant
UI, via the official Brands Proxy API. They remain the property of their
respective owners and are **not** licensed under this project's licence.

## License

GNU Affero General Public License v3.0 (AGPL-3.0) — see [`LICENSE`](LICENSE). This
covers this project's own code. Third-party code keeps its own licence (the
vendored CPD7 library is MIT — see credits), and the referenced trademarks/logos
remain the property of their owners.
EOF
echo "README written ($(wc -l < README.md) lines)"