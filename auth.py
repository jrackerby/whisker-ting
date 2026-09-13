"""AWS Cognito SRP (Secure Remote Password) authentication.

Standard algorithm, not vendor-specific to Whisker/Ting - the same group
parameters and derivation steps as every Cognito user pool (see AWS's own
`amazon-cognito-identity-js` and the widely-used `pycognito`/`warrant`
Python ports). Pure and network-adjacent-only: the crypto steps take no
HomeAssistant imports, so they are testable against fixed vectors with no
live Cognito call.

The test is `tests/test_srp.py`, run by validate.yml's `tests` job. It was
written with the component as `tools/test_whisker_ting_srp.py` in
jrackerby/HA (HA@beacbba3), deleted when that repo retired its config
surface (HA@1cc69bc6, GH-711) and lost in the extraction into this repo;
restored here by #9. What it proves is the padding, hashing and HKDF
primitives against fixed vectors - NOT a live Cognito round-trip, which
needs a real account and happens once, live, when the integration is added
through the HA UI.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import hmac
import os
from typing import Any

import aiohttp

from .const import COGNITO_CLIENT_ID, COGNITO_REGION, COGNITO_USER_POOL_ID

COGNITO_IDP_URL = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/"

# RFC 5054 2048-bit group, the fixed N/g Cognito's SRP flow uses.
N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64"
    "ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6B"
    "F12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
    "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB31"
    "43DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF"
)
G_HEX = "2"
INFO_BITS = b"Caldera Derived Key"

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class AuthenticationError(Exception):
    """Cognito rejected the credentials or the request."""


def _hash_sha256(buf: bytes) -> str:
    return hashlib.sha256(buf).hexdigest().rjust(64, "0")


def _hex_hash(hex_string: str) -> str:
    return _hash_sha256(bytearray.fromhex(hex_string))


def _pad_hex(value: int | str) -> str:
    h = value if isinstance(value, str) else f"{value:x}"
    if len(h) % 2:
        h = f"0{h}"
    elif h[0] in "89ABCDEFabcdef":
        h = f"00{h}"
    return h


def _hkdf(ikm: bytes, salt: bytes) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, INFO_BITS + b"\x01", hashlib.sha256).digest()[:16]


def _cognito_timestamp(now: datetime.datetime) -> str:
    return (
        f"{_WEEKDAYS[now.weekday()]} {_MONTHS[now.month - 1]} {now.day:d} "
        f"{now.hour:02d}:{now.minute:02d}:{now.second:02d} UTC {now.year:d}"
    )


class CognitoSRP:
    """Computes the SRP_A value and the PASSWORD_VERIFIER challenge response."""

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.pool_id = COGNITO_USER_POOL_ID
        self.big_n = int(N_HEX, 16)
        self.val_g = int(G_HEX, 16)
        self.val_k = int(_hex_hash("00" + N_HEX + "0" + G_HEX), 16)
        self._small_a = int(binascii.hexlify(os.urandom(128)).decode(), 16) % self.big_n
        self.large_a = pow(self.val_g, self._small_a, self.big_n)
        if self.large_a % self.big_n == 0:
            raise AuthenticationError("SRP safety check failed: A mod N == 0")

    def auth_params(self) -> dict[str, str]:
        return {"USERNAME": self.username, "SRP_A": f"{self.large_a:x}"}

    def _password_key(self, user_id_for_srp: str, srp_b: int, salt_hex: str) -> bytes:
        u_hex = _hex_hash(_pad_hex(self.large_a) + _pad_hex(srp_b))
        u_value = int(u_hex, 16)
        if u_value == 0:
            raise AuthenticationError("SRP safety check failed: u == 0")

        # No colon between the pool's own id and the username - Cognito's
        # own quirk, not a typo.
        identity = f"{self.pool_id.split('_')[1]}{user_id_for_srp}:{self.password}"
        identity_hash = _hash_sha256(identity.encode())
        x_value = int(_hex_hash(_pad_hex(salt_hex) + identity_hash), 16)
        g_mod_pow_x = pow(self.val_g, x_value, self.big_n)
        s_value = pow(
            srp_b - self.val_k * g_mod_pow_x,
            self._small_a + u_value * x_value,
            self.big_n,
        )
        return _hkdf(
            bytearray.fromhex(_pad_hex(s_value)),
            bytearray.fromhex(_pad_hex(f"{u_value:x}")),
        )

    def challenge_response(self, challenge_parameters: dict[str, str]) -> dict[str, str]:
        user_id_for_srp = challenge_parameters["USER_ID_FOR_SRP"]
        salt_hex = challenge_parameters["SALT"]
        srp_b = int(challenge_parameters["SRP_B"], 16)
        secret_block_b64 = challenge_parameters["SECRET_BLOCK"]

        hkdf = self._password_key(user_id_for_srp, srp_b, salt_hex)
        timestamp = _cognito_timestamp(datetime.datetime.now(datetime.timezone.utc))
        secret_block = base64.standard_b64decode(secret_block_b64)

        msg = (
            self.pool_id.split("_")[1].encode()
            + user_id_for_srp.encode()
            + secret_block
            + timestamp.encode()
        )
        signature = base64.standard_b64encode(
            hmac.new(hkdf, msg, hashlib.sha256).digest()
        ).decode()

        return {
            "TIMESTAMP": timestamp,
            "USERNAME": challenge_parameters.get("USERNAME", user_id_for_srp),
            "PASSWORD_CLAIM_SECRET_BLOCK": secret_block_b64,
            "PASSWORD_CLAIM_SIGNATURE": signature,
        }


class WhiskerAuth:
    """Cognito InitiateAuth/RespondToAuthChallenge/GetUser over plain REST."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def authenticate(self, username: str, password: str) -> dict[str, Any]:
        srp = CognitoSRP(username, password)

        init = await self._post(
            "InitiateAuth",
            {"AuthFlow": "USER_SRP_AUTH", "AuthParameters": srp.auth_params(), "ClientId": COGNITO_CLIENT_ID},
        )
        if init.get("ChallengeName") != "PASSWORD_VERIFIER":
            raise AuthenticationError(f"unexpected challenge: {init.get('ChallengeName')}")

        challenge_response = srp.challenge_response(init["ChallengeParameters"])
        result = await self._post(
            "RespondToAuthChallenge",
            {
                "ChallengeName": "PASSWORD_VERIFIER",
                "ChallengeResponses": challenge_response,
                "ClientId": COGNITO_CLIENT_ID,
            },
        )
        if "AuthenticationResult" not in result:
            raise AuthenticationError("authentication failed - no result returned")

        tokens = result["AuthenticationResult"]
        user = await self._post("GetUser", {"AccessToken": tokens["AccessToken"]})

        return {
            "access_token": tokens["AccessToken"],
            "id_token": tokens["IdToken"],
            "refresh_token": tokens["RefreshToken"],
            "user_attributes": user.get("UserAttributes", []),
        }

    async def refresh_tokens(self, refresh_token: str) -> dict[str, Any]:
        result = await self._post(
            "InitiateAuth",
            {
                "AuthFlow": "REFRESH_TOKEN_AUTH",
                "AuthParameters": {"REFRESH_TOKEN": refresh_token},
                "ClientId": COGNITO_CLIENT_ID,
            },
        )
        if "AuthenticationResult" not in result:
            raise AuthenticationError("token refresh failed - no result returned")
        return result["AuthenticationResult"]

    async def _post(self, target: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": f"AWSCognitoIdentityProviderService.{target}",
        }
        async with self._session.post(COGNITO_IDP_URL, json=payload, headers=headers) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                error_type = str(body.get("__type", ""))
                if "NotAuthorizedException" in error_type or "UserNotFoundException" in error_type:
                    raise AuthenticationError("invalid username or password")
                raise AuthenticationError(f"{target} failed: {body}")
            return body
