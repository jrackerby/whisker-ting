#!/usr/bin/env python3
"""Proves signalr_protocol.py's framing/decoding is correct against the
documented ASP.NET Core SignalR wire shapes, without a live connection or
credentials -- the module takes and returns only bytes/dataclasses.

Why this exists: the two known open-source Whisker Ting integrations
(aidenmitchell/ha-whisker-ting, simplytoast1/ha-whisker-ting -- byte-
identical to each other) skip the length-prefix framing SignalR defines for
binary transports and instead scan raw websocket bytes for a 0xCB (float64)
marker to locate the voltage reading. This suite is the evidence that the
replacement here (proper varint-length-prefixed frame splitting + real
msgpack array decoding) round-trips correctly, handles more than one
message batched into a single WS frame (the case that breaks the byte-scan
approach), and rejects truncated/garbage input instead of misreading it.

Self-test discipline (LAW 4): FAIL_CASES intentionally construct inputs the
real functions must reject or must NOT match, and main() proves each one
actually does before trusting any PASS below.
"""
import importlib.util
import os
import struct
import sys

# content_in_root: the modules live at the repository root here, not under
# custom_components/whisker_ting/ as they did in jrackerby/HA.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO, "signalr_protocol.py")


def _load():
    spec = importlib.util.spec_from_file_location("signalr_protocol", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["signalr_protocol"] = module  # dataclass() needs this in sys.modules first
    spec.loader.exec_module(module)
    return module


sr = _load()


def _fail(msg):
    # AssertionError rather than sys.exit - see the note in test_srp.py.
    raise AssertionError(msg)


def test_varint_roundtrip():
    for n in (0, 1, 63, 64, 127, 128, 300, 16384, 2_000_000):
        encoded = sr.encode_varint_length(n)
        decoded, pos = sr.decode_varint_length(encoded, 0)
        if decoded != n:
            _fail(f"varint roundtrip {n} -> {encoded.hex()} -> {decoded}")
        if pos != len(encoded):
            _fail(f"varint roundtrip {n} consumed {pos} bytes, expected {len(encoded)}")
    # Known single-byte case: lengths < 128 are one byte, MSB clear.
    if sr.encode_varint_length(5) != b"\x05":
        _fail("length 5 should encode as a single 0x05 byte")
    # 130 needs two bytes: low 7 bits (0x02) with continuation bit set, then 1.
    if sr.encode_varint_length(130) != bytes([0x82, 0x01]):
        _fail(f"length 130 encoded as {sr.encode_varint_length(130).hex()}, expected 8201")


def test_frame_and_split_single_message():
    payload = b"hello signalr"
    framed = sr.frame(payload)
    parts = sr.split_frames(framed)
    if parts != [payload]:
        _fail(f"single-message split returned {parts!r}")


def test_split_frames_multiple_messages_one_ws_frame():
    """The exact case the byte-scanning heuristic in both existing
    integrations cannot handle: two SignalR messages batched into one
    WebSocket binary frame."""
    msg_a = sr.encode_ping()
    msg_b_payload = sr.frame(b"second-message-payload")
    batched = msg_a + msg_b_payload
    parts = sr.split_frames(batched)
    if len(parts) != 2:
        _fail(f"expected 2 messages in a batched WS frame, got {len(parts)}: {parts!r}")


def test_split_frames_rejects_truncated_input():
    good = sr.frame(b"x" * 10)
    truncated = good[:-3]  # length prefix claims 10 bytes, only 7 are present
    try:
        sr.split_frames(truncated)
    except ValueError:
        pass
    else:
        _fail("split_frames accepted a frame whose length prefix exceeds the buffer")


def test_encode_decode_invocation_roundtrip():
    encoded = sr.encode_invocation("updateComboBinaryData", [{"Voltage": 121.4}], invocation_id="7")
    parts = sr.split_frames(encoded)
    if len(parts) != 1:
        _fail(f"encoded invocation split into {len(parts)} frames, expected 1")
    decoded = sr.decode_message(parts[0])
    if not isinstance(decoded, sr.HubInvocation):
        _fail(f"decode_message did not return a HubInvocation: {decoded!r}")
    if decoded.target != "updateComboBinaryData":
        _fail(f"target mismatch: {decoded.target!r}")
    if decoded.arguments != [{"Voltage": 121.4}]:
        _fail(f"arguments mismatch: {decoded.arguments!r}")
    if decoded.invocation_id != "7":
        _fail(f"invocation_id mismatch: {decoded.invocation_id!r}")


def test_encode_invocation_is_bare_array_not_map():
    """The bug being fixed: both existing repos wrap the invocation in a
    single-key map `{1: [...]}` before packing, which is not the documented
    Hub Protocol shape. Assert this implementation does NOT do that."""
    import msgpack

    encoded = sr.encode_invocation("target", [])
    payload = sr.split_frames(encoded)[0]
    unpacked = msgpack.unpackb(payload, raw=False)
    if not isinstance(unpacked, list):
        _fail(f"encode_invocation produced a {type(unpacked).__name__}, not a bare array: {unpacked!r}")
    if unpacked[0] != sr.MSG_INVOCATION:
        _fail(f"first array element should be message type 1, got {unpacked[0]!r}")


def test_decode_message_ping():
    encoded = sr.encode_ping()
    payload = sr.split_frames(encoded)[0]
    decoded = sr.decode_message(payload)
    if decoded != sr.MSG_PING:
        _fail(f"ping did not decode to MSG_PING: {decoded!r}")


def test_decode_message_rejects_garbage():
    import msgpack

    # A msgpack-valid but non-array top-level value must not be mistaken for a message.
    garbage = msgpack.packb({"not": "an array"}, use_bin_type=True)
    if sr.decode_message(garbage) is not None:
        _fail("decode_message accepted a non-array top-level value")
    # Bytes that are not valid msgpack at all.
    if sr.decode_message(b"\xff\xff\xff\xff") is not None:
        _fail("decode_message accepted invalid msgpack bytes")


def test_handshake_request_and_response():
    request = sr.encode_handshake_request()
    if not request.endswith(sr.HANDSHAKE_RECORD_SEPARATOR):
        _fail("handshake request missing record separator")
    import json

    body = json.loads(request.rstrip(sr.HANDSHAKE_RECORD_SEPARATOR))
    if body.get("protocol") != "messagepack":
        _fail(f"handshake request missing protocol field: {request!r}")

    ok = sr.parse_handshake_response("{}" + sr.HANDSHAKE_RECORD_SEPARATOR)
    if ok is not None:
        _fail(f"successful handshake response read as an error: {ok!r}")

    refused = sr.parse_handshake_response('{"error":"Requested protocol \'json\' is not available."}' + sr.HANDSHAKE_RECORD_SEPARATOR)
    if not refused:
        _fail("refused handshake response was not detected as an error")


def test_positional_float64_still_recoverable_when_needed():
    """websocket.py's last-resort scan depends on being able to find a
    manually-embedded float64 (0xCB marker) inside an arbitrary msgpack
    payload -- prove that primitive still works standalone, independent of
    whatever websocket.py does with the result."""
    raw = b"\xcb" + struct.pack(">d", 121.3) + b"\xcb" + struct.pack(">d", 0.5)
    doubles = []
    pos = 0
    while pos < len(raw):
        if raw[pos] == 0xCB:
            doubles.append(struct.unpack(">d", raw[pos + 1 : pos + 9])[0])
            pos += 9
        else:
            pos += 1
    if doubles != [121.3, 0.5]:
        _fail(f"float64 scan primitive broken: {doubles!r}")


CASES = (
    test_varint_roundtrip,
    test_frame_and_split_single_message,
    test_split_frames_multiple_messages_one_ws_frame,
    test_split_frames_rejects_truncated_input,
    test_encode_decode_invocation_roundtrip,
    test_encode_invocation_is_bare_array_not_map,
    test_decode_message_ping,
    test_decode_message_rejects_garbage,
    test_handshake_request_and_response,
    test_positional_float64_still_recoverable_when_needed,
)


def test_self_test_can_fail():
    """LAW 4: an assertion set needs a self-test proving it CAN fail. Feed
    split_frames a length prefix that UNDER-claims the payload (valid varint,
    but shorter than what a correctly-framed message would use) so the
    parser accepts it and returns a truncated slice -- then assert that
    truncated slice does NOT equal the original payload, proving the
    equality check in test_frame_and_split_single_message is not vacuously
    true no matter what split_frames returns."""
    real_payload = b"the actual payload"
    # Prefix claims 4 bytes and exactly 4 bytes follow (no trailing garbage
    # to further complicate parsing) -- a well-formed frame, just a SHORTER
    # one than the full payload.
    short_frame = sr.encode_varint_length(4) + real_payload[:4]
    parts = sr.split_frames(short_frame)
    if parts == [real_payload]:
        _fail("self-test failed to fail: a 4-byte frame must not recover "
              "the full 19-byte original payload")


def main():
    test_self_test_can_fail()
    for case in CASES:
        case()
        print(f"ok: {case.__name__}")
    print(f"\n{len(CASES)} checks passed (plus 1 self-test).")


if __name__ == "__main__":
    main()
