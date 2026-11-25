"""Shared mTLS SSLContext helpers using Python standard library only."""

import ssl
from typing import Optional


def create_server_ssl_context(
    cert_file: str,
    key_file: str,
    ca_file: str,
    require_client_cert: bool = True,
) -> ssl.SSLContext:
    """Create a TLS 1.2+ server SSLContext with optional mandatory client cert verification."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
    ctx.load_verify_locations(cafile=ca_file)
    ctx.verify_mode = ssl.CERT_REQUIRED if require_client_cert else ssl.CERT_OPTIONAL
    return ctx


def create_client_ssl_context(
    ca_file: str,
    cert_file: Optional[str] = None,
    key_file: Optional[str] = None,
    check_hostname: bool = True,
) -> ssl.SSLContext:
    """Create a TLS 1.2+ client SSLContext presenting a client cert for mTLS."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = check_hostname
    ctx.load_verify_locations(cafile=ca_file)
    if cert_file and key_file:
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
    return ctx
