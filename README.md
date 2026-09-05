# Whisker Ting

Home Assistant integration for Whisker/Ting devices, over the vendor cloud.

The transport is SignalR — `signalr_protocol.py` and `websocket.py` implement
it directly rather than pulling a client library, and `auth.py` handles the
token exchange. `iot_class` is `cloud_push`: state arrives on the socket, and
the scan interval is a fallback, not the primary path.

## What it creates

Platforms: `binary_sensor`, `sensor`. One device per account device.

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

> **That path has two owners today.** `jrackerby/HA` also submodules this repo
> as `custom_components/whisker_ting` and writes the same directory on deploy. Until
> that cutover is settled (jrackerby/HA#483), a HACS install and a `git push ha
> master` will fight over it — install here only if you are not deploying this
> component from `jrackerby/HA`.

## Development

Issues and feature requests: **[jrackerby/whisker-ting/issues](https://github.com/jrackerby/whisker-ting/issues)**.

CI runs [hassfest](https://developers.home-assistant.io/blog/2020/04/16/hassfest)
and HACS validation on every push. hassfest scans `custom_components/*` and
takes no path argument, so `.github/workflows/validate.yml` stages this repo
into that layout before invoking it; the repo itself stays root-layout because
`jrackerby/HA` submodules it at that path.

Pushing a `manifest.json` whose `version` has changed tags and publishes a
release automatically — that is the only supported way to cut one.
