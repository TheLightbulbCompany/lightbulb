"""Cryptographic helpers for backend↔OpenClaw device authentication.

The OpenClaw 4.5 gateway introduced a scoped-auth system where the gateway
token is only a transport-level shared secret; to receive any `operator.*`
scopes the backend must present a signed Ed25519 device identity in the
connect request. These helpers implement the client side of that handshake.
"""
