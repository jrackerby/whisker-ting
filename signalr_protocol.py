"""ASP.NET Core SignalR MessagePack hub protocol - framing and encoding.

Pure and network-free: bytes in, bytes/objects out, so tools/
test_whisker_ting_signalr.py covers it without a live connection.

Spec: aspnetcore SignalR's "Binary Message Framing Format" - every message
on a binary transport, WebSocket included, is preceded by a length prefix
encoded as a 7-bit varint (LEB128 shape, same as protobuf's). A single
WebSocket frame is allowed to carry more than one framed message back to
back; nothing about WS being message-oriented exempts the payload from this
framing.

Both known open-source Whisker Ting integrations (aidenmitchell/
ha-whisker-ting and simplytoast1/ha-whisker-ting - byte-identical to each
other, comments included) skip this framing on both send and receive: they
push a bare `{1: [...]}` map through `send_bytes` with no length prefix, and
on receive they never split a frame into its component messages - they scan
the raw bytes of whatever `msg.data` was for a 0xCB (float64) marker and
grab the first four hits positionally. That breaks the moment the server
batches two messages into one WS frame (the scanner walks into the second
message's bytes) or changes field count/order (the scanner has no concept
of a field). This module parses the actual message instead: split_frames()
respects the length prefix, decode_message() unpacks a real msgpack array
and returns a typed HubInvocation with its target and arguments intact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import msgpack

MSG_INVOCATION = 1
MSG_STREAM_ITEM = 2
MSG_COMPLETION = 3
MSG_STREAM_INVOCATION = 4
MSG_CANCEL_INVOCATION = 5
MSG_PING = 6
MSG_CLOSE = 7

HANDSHAKE_RECORD_SEPARATOR = "\x1e"


def encode_varint_length(n: int) -> bytes:
    """LEB128 7-bit varint, as SignalR's BinaryMessageFormatter writes it."""
    if n < 0:
        raise ValueError("length must be non-negative")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def decode_varint_length(data: bytes, pos: int) -> tuple[int, int]:
    """Returns (length, next_pos). Raises ValueError on a truncated prefix."""
    length = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint length prefix")
        b = data[pos]
        pos += 1
        length |= (b & 0x7F) << shift
        if not (b & 0x80):
            return length, pos
        shift += 7


def frame(message: bytes) -> bytes:
    """Length-prefix one already-msgpack-encoded message for the wire."""
    return encode_varint_length(len(message)) + message


def split_frames(data: bytes) -> list[bytes]:
    """Split a raw binary WS payload into its framed messages."""
    out: list[bytes] = []
    pos = 0
    n = len(data)
    while pos < n:
        length, pos = decode_varint_length(data, pos)
        if pos + length > n:
            raise ValueError("frame length exceeds remaining buffer")
        out.append(data[pos : pos + length])
        pos += length
    return out


@dataclass
class HubInvocation:
    """A server->client method call (message type 1)."""

    target: str
    arguments: list[Any]
    headers: dict[str, str]
    invocation_id: str | None


def decode_message(payload: bytes) -> HubInvocation | int | None:
    """Unpack one de-framed message. Returns a HubInvocation for an
    Invocation (type 1), the raw message-type int for anything else
    recognizable (ping=6, close=7, ...), or None if it does not unpack as a
    non-empty array at all.
    """
    try:
        arr = msgpack.unpackb(payload, raw=False, strict_map_key=False)
    except Exception:
        return None
    if not isinstance(arr, list) or not arr:
        return None
    msg_type = arr[0]
    if not isinstance(msg_type, int):
        return None
    if msg_type != MSG_INVOCATION:
        return msg_type
    # [1, headers, invocationId, target, arguments, streamIds?]
    if len(arr) < 5 or not isinstance(arr[3], str):
        return None
    headers = arr[1] if isinstance(arr[1], dict) else {}
    invocation_id = arr[2] if isinstance(arr[2], str) else None
    arguments = arr[4] if isinstance(arr[4], list) else [arr[4]]
    return HubInvocation(target=arr[3], arguments=arguments, headers=headers, invocation_id=invocation_id)


def encode_invocation(target: str, arguments: list[Any], invocation_id: str | None = None) -> bytes:
    """Encode + frame a client->server invocation. A bare top-level array
    per the documented Hub Protocol shape - not the `{1: [...]}` single-key
    map both existing repos send instead."""
    arr: list[Any] = [MSG_INVOCATION, {}, invocation_id, target, arguments]
    return frame(msgpack.packb(arr, use_bin_type=True))


def encode_ping() -> bytes:
    return frame(msgpack.packb([MSG_PING], use_bin_type=True))


def encode_handshake_request(protocol: str = "messagepack", version: int = 1) -> str:
    import json

    return json.dumps({"protocol": protocol, "version": version}) + HANDSHAKE_RECORD_SEPARATOR


def parse_handshake_response(text: str) -> str | None:
    """Returns an error message if the handshake was refused, else None."""
    import json

    body = text.split(HANDSHAKE_RECORD_SEPARATOR, 1)[0]
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except ValueError:
        return f"unparseable handshake response: {body!r}"
    error = parsed.get("error") if isinstance(parsed, dict) else None
    return str(error) if error else None
