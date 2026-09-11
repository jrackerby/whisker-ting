# Whisker Ting

Home Assistant integration for the **Ting** electrical fire sensor by Whisker
Labs — the plug-in monitor that watches a home's wiring for arc faults and
reports a fire hazard before it becomes one. (Not the Whisker that makes
Litter-Robot; different company, same word.)

## How it gets the data

Ting has **no local API**. The plug talks only to Whisker Labs' cloud, and
everything a customer sees comes back out of it, so this integration signs in
as the phone app does and reads the same three places:

- **AWS Cognito** for authentication, exchanged in `auth.py`.
- **A REST endpoint** for device state — hazard flags, firmware, HVAC
  verification, learning-mode status.
- **A SignalR hub** for the live voltage stream. `signalr_protocol.py` and
  `websocket.py` implement the SignalR framing directly rather than pulling a
  client library, so messages are decoded against the protocol's own framing
  instead of by scanning raw bytes for float markers.

`iot_class` is `cloud_push`: state arrives on the socket as it happens, and the
configured scan interval is a fallback for when the socket is quiet, not the
primary path.

**This is reverse-engineered, and the endpoint facts are not this project's
discovery.** They were recovered independently by the
`aidenmitchell/ha-whisker-ting` and `simplytoast1/ha-whisker-ting` projects,
whose implementations agree with each other; this one reuses those endpoints
and replaces the websocket client. Whisker Labs does not document or support
any of it, and can change it without notice.

## What it creates

One device per Ting on the account.

| entity | |
|---|---|
| `binary_sensor` fire hazard | the headline hazard flag |
| `binary_sensor` electrical fault hazard | with `sensor` electrical fault hazard status carrying the vendor's message |
| `binary_sensor` unsafe frequency hazard | with its own status message sensor |
| `binary_sensor` frozen pipe | `device_class: cold` |
| `binary_sensor` learning mode | the plug is still characterising the home's electrical signature |
| `binary_sensor` HVAC verified | |
| `sensor` voltage, voltage high, voltage low | live, from the SignalR stream |
| `sensor` average peaks max | |
| `sensor` firmware version | diagnostic |

## Known limitations

- **Cloud only.** No account, no internet, no entities. There is no local path
  to fall back to.
- **The live-voltage message shape is inferred.** The push carries four float64
  values whose order is not documented. `websocket.py` tries structured field
  names first and falls back to positional floats, logging at `WARNING` when it
  has to guess — so check the log once after installing to see which path your
  account took.
- **Learning mode matters.** A newly installed Ting reports no useful hazard
  state until it has characterised the home; the `learning_mode` sensor is
  there so an automation can tell that apart from "nothing is wrong".

## Configuration

Config flow. Required: username, password, and a scan interval.

Requires `msgpack`, declared in the manifest and installed by Home Assistant.

## Install

**Via HACS.** HACS → ⋮ → *Custom repositories* → `https://github.com/jrackerby/whisker-ting`,
category **Integration**. Install, restart Home Assistant, then add it under
*Settings → Devices & Services → Add Integration → "Whisker Ting"*.

The integration lives at the repository **root**, not under
`custom_components/`. `hacs.json` declares `content_in_root: true`, so HACS
copies the root into `/config/custom_components/whisker_ting/`.

## Development

Issues and feature requests: **[jrackerby/whisker-ting/issues](https://github.com/jrackerby/whisker-ting/issues)**.

CI runs [hassfest](https://developers.home-assistant.io/blog/2020/04/16/hassfest)
and HACS validation on every push. hassfest scans `custom_components/*` and
takes no path argument, so `.github/workflows/validate.yml` stages this repo
into that layout before invoking it; the repo itself stays root-layout because
`hacs.json` declares `content_in_root: true`.

Pushing a `manifest.json` whose `version` has changed tags and publishes a
release automatically — that is the only supported way to cut one.
