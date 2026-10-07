# Third-party code

## custom_components/niko_access/cpd7/

The local CPD7 streaming client (`cas.py`, `crypto.py`, `hik_decoder.py`,
`lan_client.py`, `_const.py`, `cas_ca_bundle.pem`) and the CPD7 streaming
pipeline in `camera.py` are adapted from:

- **rtammekivi/hikconnect_intercom** — https://github.com/rtammekivi/hikconnect_intercom
  MIT License, Copyright (c) 2026 Silvio Vaira (Bobsilvio).

The CAS client (`cpd7/cas.py`) derives from **pyezvizapi**, also MIT-licensed.

These reverse-engineer Hikvision/EZVIZ (Hik-Connect) protocols for
interoperability with hardware the user owns.
