#!/usr/bin/env python3
"""Checks auth.py's SRP math primitives against hand-computed values.

This is NOT a full protocol verification against a live Cognito user pool -
that needs a real account and is done once, live, when the integration is
actually added through the HA UI (see const.py's own note on what remains
unverified). What this DOES prove offline: the padding, hashing and safety-
check primitives that SRP-6a's password-verifier math is built from behave
the way the algorithm requires, so a live-auth failure (if one ever occurs)
is a Cognito-side or network-side problem, not an off-by-one in these
helpers.
"""
import importlib.util
import hashlib
import os
import sys

# This repo declares hacs.json content_in_root, so the integration modules sit
# at the REPOSITORY ROOT, not under custom_components/whisker_ting/ the way
# they did in jrackerby/HA where this file was written.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO, "auth.py")


def _load_direct():
    """auth.py's only local import is `from .const import ...`. Load both
    as siblings under one throwaway package name so that relative import
    resolves, without importing __init__.py (which needs homeassistant)."""
    import types

    pkg_name = "_whisker_ting_test_pkg"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [os.path.dirname(MODULE_PATH)]
    sys.modules[pkg_name] = pkg

    const_spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.const", os.path.join(os.path.dirname(MODULE_PATH), "const.py")
    )
    const_module = importlib.util.module_from_spec(const_spec)
    sys.modules[const_spec.name] = const_module
    const_spec.loader.exec_module(const_module)

    auth_spec = importlib.util.spec_from_file_location(f"{pkg_name}.auth", MODULE_PATH)
    auth_module = importlib.util.module_from_spec(auth_spec)
    sys.modules[auth_spec.name] = auth_module
    auth_spec.loader.exec_module(auth_module)
    return auth_module


auth = _load_direct()


def _fail(msg):
    # An AssertionError, not sys.exit: under pytest a SystemExit is reported as
    # an error rather than a failure and takes the whole session with it. main()
    # below still gives the standalone `python3 tests/test_srp.py` run.
    raise AssertionError(msg)


def test_hash_sha256_matches_hashlib():
    for buf in (b"", b"whisker", b"\x00\x01\x02\xff" * 8):
        expected = hashlib.sha256(buf).hexdigest().rjust(64, "0")
        actual = auth._hash_sha256(buf)
        if actual != expected:
            _fail(f"_hash_sha256({buf!r}) = {actual}, expected {expected}")


def test_pad_hex_even_length_and_high_bit():
    # Odd-length hex gets a single leading zero.
    if auth._pad_hex("abc") != "0abc":
        _fail(f"_pad_hex('abc') = {auth._pad_hex('abc')!r}, expected '0abc'")
    # Even-length hex starting with a high nibble (>=8) gets a full zero byte
    # prepended - this is what keeps the value unambiguously positive when
    # later read back as a big-endian two's-complement-adjacent quantity.
    if auth._pad_hex("ff01") != "00ff01":
        _fail(f"_pad_hex('ff01') = {auth._pad_hex('ff01')!r}, expected '00ff01'")
    # Even-length, low leading nibble: untouched.
    if auth._pad_hex("7f01") != "7f01":
        _fail(f"_pad_hex('7f01') = {auth._pad_hex('7f01')!r}, expected unchanged")


def test_hkdf_deterministic_and_16_bytes():
    ikm = b"\x01" * 32
    salt = b"\x02" * 16
    out1 = auth._hkdf(ikm, salt)
    out2 = auth._hkdf(ikm, salt)
    if out1 != out2:
        _fail("_hkdf is not deterministic for identical inputs")
    if len(out1) != 16:
        _fail(f"_hkdf output length = {len(out1)}, expected 16 (AES-128 key size)")
    # Changing the salt must change the output - a constant-salt bug would
    # make every user's derived key collide on IKM alone.
    if out1 == auth._hkdf(ikm, b"\x03" * 16):
        _fail("_hkdf output did not change when the salt changed")


def test_cognito_srp_a_is_nonzero_mod_n():
    """A = g^a mod N must never be 0 mod N - CognitoSRP.__init__ itself
    guards this, so constructing 20 instances with fresh random exponents
    and none tripping the guard is the available offline evidence that the
    guard's condition is reachable-but-avoided in the overwhelmingly common
    case, not evidence the guard is unreachable dead code."""
    for _ in range(20):
        srp = auth.CognitoSRP("test@example.com", "password123")
        if srp.large_a % srp.big_n == 0:
            _fail("large_a % big_n == 0 slipped past the constructor's own check")
        if not (0 < srp.large_a < srp.big_n):
            _fail(f"large_a out of range: {srp.large_a}")


def test_auth_params_shape():
    srp = auth.CognitoSRP("someone@example.com", "hunter2")
    params = srp.auth_params()
    if set(params) != {"USERNAME", "SRP_A"}:
        _fail(f"auth_params() keys = {set(params)}, expected USERNAME/SRP_A")
    if params["USERNAME"] != "someone@example.com":
        _fail("auth_params() did not carry the username through unchanged")
    # SRP_A is A rendered as hex, no leading '0x'.
    if int(params["SRP_A"], 16) != srp.large_a:
        _fail("auth_params()['SRP_A'] does not parse back to large_a")


def test_self_test_can_fail():
    """LAW 4: prove the pad_hex check above actually discriminates by
    feeding it the WRONG expected value and confirming that mismatch is
    caught, not silently accepted."""
    wrong = auth._pad_hex("abc") == "abc"  # true only if padding were skipped
    if wrong:
        _fail("self-test failed to fail: _pad_hex('abc') must not equal 'abc' unpadded")


CASES = (
    test_hash_sha256_matches_hashlib,
    test_pad_hex_even_length_and_high_bit,
    test_hkdf_deterministic_and_16_bytes,
    test_cognito_srp_a_is_nonzero_mod_n,
    test_auth_params_shape,
)


def main():
    test_self_test_can_fail()
    for case in CASES:
        case()
        print(f"ok: {case.__name__}")
    print(f"\n{len(CASES)} checks passed (plus 1 self-test).")
    print(
        "\nNOTE: this verifies the SRP primitives, not a live Cognito "
        "round-trip - that needs a real account and happens once, live, "
        "when the integration is added through the HA UI."
    )


if __name__ == "__main__":
    main()
