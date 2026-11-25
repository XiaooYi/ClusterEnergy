#!/usr/bin/env python3
"""Generate offline CA and mTLS certificates for C1, C2, A, and B nodes."""

import datetime
import ipaddress
import os
from typing import List, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CERTS_DIR = os.path.join(ROOT_DIR, "certs")


def gen_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def save_key(key, path: str) -> None:
    with open(path, "wb") as f:
        f.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )


def save_cert(cert, path: str) -> None:
    with open(path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def create_ca():
    key = gen_key()
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ClusterEnergy-Offline-CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def sign_leaf(
    ca_key,
    ca_cert,
    cn: str,
    eku: ExtendedKeyUsageOID,
    ips: Optional[List[str]] = None,
    dns_names: Optional[List[str]] = None,
):
    key = gen_key()
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1825))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
    )
    san_entries = []
    for ip in ips or []:
        san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
    for d in dns_names or []:
        san_entries.append(x509.DNSName(d))
    if san_entries:
        builder = builder.add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
    cert = builder.sign(ca_key, hashes.SHA256())
    return key, cert


def main() -> int:
    os.makedirs(CERTS_DIR, exist_ok=True)
    ca_key, ca_cert = create_ca()
    save_key(ca_key, os.path.join(CERTS_DIR, "ca.key"))
    save_cert(ca_cert, os.path.join(CERTS_DIR, "ca.crt"))

    specs = [
        ("ln2", ExtendedKeyUsageOID.SERVER_AUTH, ["25.8.100.22"], ["ln2"]),
        ("ln4", ExtendedKeyUsageOID.SERVER_AUTH, ["25.8.100.24"], ["ln4"]),
        ("a_client", ExtendedKeyUsageOID.CLIENT_AUTH, None, ["a-client"]),
        ("a_server", ExtendedKeyUsageOID.SERVER_AUTH, ["127.0.0.1", "25.8.100.23"], ["ln3", "localhost"]),
        ("b_client", ExtendedKeyUsageOID.CLIENT_AUTH, None, ["b-client"]),
    ]

    for name, eku, ips, dns in specs:
        k, c = sign_leaf(ca_key, ca_cert, name, eku, ips, dns)
        save_key(k, os.path.join(CERTS_DIR, f"{name}.key"))
        save_cert(c, os.path.join(CERTS_DIR, f"{name}.crt"))

    print(f"Generated offline CA and {len(specs)} certificates in {CERTS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
