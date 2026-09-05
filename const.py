"""Constants for the Whisker Ting integration.

WHAT THIS IS. Whisker Labs' Ting is an electrical fire/arc-fault sensor with
no documented local API - everything lives behind their AWS-hosted cloud
(Cognito auth, a REST device-state endpoint, an Azure/ASP.NET Core SignalR
hub for the real-time voltage stream). Two independent GitHub projects
(aidenmitchell/ha-whisker-ting, simplytoast1/ha-whisker-ting) reverse-
engineered this and are byte-identical to each other down to the comments -
same Cognito pool/client IDs, same REST shape, same SignalR hub URL. This
integration reuses those endpoint facts (there is only one real backend to
point at) but replaces their SignalR client, which skips the protocol's own
message framing and instead scans raw websocket bytes for a 0xCB (float64)
marker to guess where the voltage values are. See signalr_protocol.py.

UNVERIFIED AGAINST LIVE TRAFFIC. The endpoint URLs and the REST device JSON
shape are corroborated by two independent reverse-engineering efforts and
are trusted. The exact SignalR `arguments` structure for the
`updateComboBinaryData` push is not independently confirmed - the previous
two projects only ever recovered it as "four raw float64s in some order,
found by scanning." websocket.py tries structured field names first, falls
back to positional floats, and logs at WARNING when it has to guess, so a
live install surfaces which path it took instead of silently trusting one.

BEFORE THIS DOMAIN IS TRUSTED FOR household_state OR fls_monitoring: add it
to a session's estate check, watch the log for the WARNING above at least
once, and confirm sensor.<x>_voltage moves under real load. Until then
fls_monitoring's Ting row stays on device_tracker reachability only (see its
own comment) - this integration does not replace that on its own.
"""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "whisker_ting"

# AWS Cognito - one user pool for the whole Whisker/Ting product, same
# constants independently recovered by both prior projects.
COGNITO_REGION = "us-east-1"
COGNITO_USER_POOL_ID = "us-east-1_trW4gH661"
COGNITO_CLIENT_ID = "4akjeqt9gtl8rgg1cksunipk9u"

API_BASE_URL = "https://api.wskr.io"
API_USERS_ENDPOINT = "/api/v1/Users/{user_id}"

SIGNALR_URL = "wss://signalr.api.wskr.io/dataHub"
SIGNALR_ORIGIN = "ionic://localhost"  # the mobile app's own origin; the hub 403s without it

CONF_USERNAME = "username"
CONF_PASSWORD = "password"

DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30
MAX_SCAN_INTERVAL = 3600
CONF_SCAN_INTERVAL = "scan_interval"

UPDATE_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL)

# Same reasoning as host_monitor/kiosk_pi: a transient miss must not read as
# a health problem before it has actually persisted.
TRANSPORT_FAIL_DWELL = 3

# No update in this long on a connected socket means the socket is dead in
# a way the transport didn't tell us about yet (normal cadence is ~250ms).
STALE_DATA_THRESHOLD = 30

RECONNECT_MIN_DELAY = 5
RECONNECT_MAX_DELAY = 300
RECONNECT_BACKOFF_FACTOR = 2

PLATFORMS = ["binary_sensor", "sensor"]

NO_HAZARD_MESSAGE = "No Hazards Detected"
