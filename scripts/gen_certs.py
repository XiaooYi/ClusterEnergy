#!/usr/bin/env python3
"""Generate offline CA and mTLS certificates for C1, C2, A, and B nodes."""

import argparse
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
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as f:
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


def split_sans(hosts):
    ips, names = [], []
    for host in hosts:
        try:
            ips.append(str(ipaddress.ip_address(host)))
        except ValueError:
            names.append(host)
    return ips, names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=CERTS_DIR)
    parser.add_argument("--a-host", action="append", help="repeat for each A host name or IP")
    parser.add_argument("--c-server", action="append", help="repeat name=host for each C endpoint")
    args = parser.parse_args()
    output = os.path.abspath(args.output)
    if os.path.exists(os.path.join(output, "ca.key")):
        parser.error("choose a new output directory to avoid replacing an existing CA")
    a_hosts = args.a_host or ["127.0.0.1", "localhost"]
    c_hosts = args.c_server or []
    c_specs = {}
    for value in c_hosts:
        name, separator, host = value.partition("=")
        if not separator or not name or not host or not name.replace("_", "").replace("-", "").isalnum():
            parser.error("--c-server must be name=host, with a simple certificate name")
        if name in {"ca", "a_server", "a_client", "b_client"}:
            parser.error(f"reserved certificate name: {name}")
        c_specs.setdefault(name, []).append(host)

    os.makedirs(output, mode=0o700, exist_ok=True)
    ca_key, ca_cert = create_ca()
    save_key(ca_key, os.path.join(output, "ca.key"))
    save_cert(ca_cert, os.path.join(output, "ca.crt"))

    specs = [
        ("a_client", ExtendedKeyUsageOID.CLIENT_AUTH, None, ["a-client"]),
        ("a_server", ExtendedKeyUsageOID.SERVER_AUTH, *split_sans(a_hosts)),
        ("b_client", ExtendedKeyUsageOID.CLIENT_AUTH, None, ["b-client"]),
    ]
    specs.extend((name, ExtendedKeyUsageOID.SERVER_AUTH, *split_sans(hosts))
                 for name, hosts in c_specs.items())

    for name, eku, ips, dns in specs:
        k, c = sign_leaf(ca_key, ca_cert, name, eku, ips, dns)
        save_key(k, os.path.join(output, f"{name}.key"))
        save_cert(c, os.path.join(output, f"{name}.crt"))

    print(f"Generated offline CA and {len(specs)} certificates in {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
