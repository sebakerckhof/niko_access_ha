"""CAS TLS: an expired certificate is tolerated only when everything else verifies."""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from custom_components.niko_access.cpd7 import cas
from custom_components.niko_access.cpd7._const import PyEzvizError

HOST = "127.0.0.1"
SESSION_ID = "cloud-session-id"
DEVICE_KEY = "DEVICEKEY0123456"
NOW = dt.datetime.now(dt.UTC)
DAY = dt.timedelta(days=1)
VALID = (NOW - 30 * DAY, NOW + 30 * DAY)
EXPIRED = (NOW - 400 * DAY, NOW - 10 * DAY)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(
    subject: str,
    key: ec.EllipticCurvePrivateKey,
    issuer: str,
    issuer_key: ec.EllipticCurvePrivateKey,
    validity: tuple[dt.datetime, dt.datetime],
    *,
    signed_by: ec.EllipticCurvePrivateKey | None = None,
    san: x509.GeneralName | None = None,
) -> x509.Certificate:
    """A CA certificate, or with ``san`` a server certificate.

    ``signed_by`` signs with a key other than the named issuer's: a forgery.
    """
    ca = san is None
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(validity[0])
        .not_valid_after(validity[1])
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=not ca,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
    )
    if san is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([san]), critical=False
        ).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    return builder.sign(signed_by or issuer_key, hashes.SHA256())


def _pem(*certs: x509.Certificate) -> bytes:
    return b"".join(cert.public_bytes(serialization.Encoding.PEM) for cert in certs)


def _der(*certs: x509.Certificate) -> list[bytes]:
    return [cert.public_bytes(serialization.Encoding.DER) for cert in certs]


class Pki:
    """A root CA that the CAS client trusts, and certificates below it."""

    def __init__(self) -> None:
        self.root_key = ec.generate_private_key(ec.SECP256R1())
        self.root = _certificate(
            "Test Root", self.root_key, "Test Root", self.root_key, (NOW - 3650 * DAY, NOW + DAY)
        )
        self.old_ca_key = ec.generate_private_key(ec.SECP256R1())
        # Genuinely issued by the root, and expired: anyone can replay such a certificate.
        self.old_ca = _certificate(
            "Old Intermediate",
            self.old_ca_key,
            "Test Root",
            self.root_key,
            (NOW - 900 * DAY, EXPIRED[1]),
        )
        self.server_key = ec.generate_private_key(ec.SECP256R1())

    def leaf(self, validity, san=None, **kwargs) -> x509.Certificate:
        """A server certificate issued by the root."""
        san = san or x509.IPAddress(ipaddress.ip_address(HOST))
        return _certificate(
            "cas", self.server_key, "Test Root", self.root_key, validity, san=san, **kwargs
        )

    def forged_leaf(self, san=None) -> x509.Certificate:
        """Names the expired intermediate as its issuer, but is signed by another key."""
        san = san or x509.IPAddress(ipaddress.ip_address(HOST))
        return _certificate(
            "cas",
            self.server_key,
            "Old Intermediate",
            self.old_ca_key,
            EXPIRED,
            signed_by=ec.generate_private_key(ec.SECP256R1()),
            san=san,
        )

    def self_signed_leaf(self) -> x509.Certificate:
        return _certificate(
            "cas",
            self.server_key,
            "cas",
            self.server_key,
            EXPIRED,
            san=x509.IPAddress(ipaddress.ip_address(HOST)),
        )


@pytest.fixture
def pki(tmp_path, monkeypatch) -> Pki:
    pki = Pki()
    bundle = tmp_path / "ca_bundle.pem"
    bundle.write_bytes(_pem(pki.root))
    monkeypatch.setattr(cas, "CAS_CA_BUNDLE", bundle)
    return pki


class FakeCas:
    """A TLS server that answers any request with a device key, and records the requests."""

    def __init__(self, certfile, keyfile) -> None:
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(certfile, keyfile)
        self._listener = socket.create_server((HOST, 0))
        self._listener.settimeout(0.05)
        self.port = self._listener.getsockname()[1]
        self.requests: list[bytes] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run)
        self._thread.start()

    def _run(self) -> None:
        body = f'<Response><Session Key="{DEVICE_KEY}" OperationCode="op"/></Response>'.encode()
        response = (
            cas._cas_frame_header(
                sequence=5, command=cas.CAS_COMMAND_GET_OPERATION_CODE, body_size_hint=len(body)
            )
            + body
            + b"0" * cas.CAS_RESPONSE_DIGEST_SIZE
        )
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            with conn:
                conn.settimeout(2)
                try:
                    with self._context.wrap_socket(conn, server_side=True) as tls:
                        request = tls.recv(4096)
                        if request:
                            self.requests.append(request)
                            tls.sendall(response)
                except OSError:
                    pass  # the client refused the certificate

    def close(self) -> None:
        self._stop.set()
        self._thread.join()
        self._listener.close()


@pytest.fixture
def serve(pki, tmp_path, socket_enabled):
    """Start a fake CAS server presenting the given certificate chain."""
    servers: list[FakeCas] = []
    keyfile = tmp_path / "server_key.pem"
    keyfile.write_bytes(
        pki.server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    def start(*chain: x509.Certificate) -> FakeCas:
        certfile = tmp_path / f"chain_{len(servers)}.pem"
        certfile.write_bytes(_pem(*chain))
        servers.append(FakeCas(certfile, keyfile))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def fetch_key(server: FakeCas, **kwargs) -> str:
    """The call camera.py makes to get the door station's control key."""
    token = {
        "session_id": SESSION_ID,
        "service_urls": {"sysConf": [None] * 15 + [HOST, server.port]},
    }
    doc = cas.EzvizCAS(token, **kwargs).cas_get_encryption("Q00000000")
    return doc["Response"]["Session"]["@Key"]


def test_valid_certificate(pki, serve):
    server = serve(pki.leaf(VALID))
    assert fetch_key(server) == DEVICE_KEY


def test_expired_certificate_is_tolerated(pki, serve):
    """The vendor lets CAS certificates lapse; the apps keep working, and so does this."""
    server = serve(pki.leaf(EXPIRED))
    assert fetch_key(server) == DEVICE_KEY
    assert SESSION_ID.encode() in server.requests[0]


def test_expired_certificate_can_be_refused(pki, serve):
    server = serve(pki.leaf(EXPIRED))
    with pytest.raises(PyEzvizError):
        fetch_key(server, allow_expired_certificate=False)
    assert server.requests == []


def test_forged_certificate_below_an_expired_ca_is_rejected(pki, serve):
    """An expired CA certificate fails the handshake for expiry before the leaf is checked.

    The retry must not take that as proof that only the date is wrong.
    """
    server = serve(pki.forged_leaf(), pki.old_ca)
    with pytest.raises(PyEzvizError, match="not valid for"):
        fetch_key(server)
    assert server.requests == []  # the session ID never left


def test_self_signed_expired_certificate_is_rejected(pki, serve):
    server = serve(pki.self_signed_leaf())
    with pytest.raises(PyEzvizError):
        fetch_key(server)
    assert server.requests == []


def test_responses_are_not_logged(pki, serve, caplog):
    caplog.set_level(logging.DEBUG)
    assert fetch_key(serve(pki.leaf(VALID))) == DEVICE_KEY
    assert "CAS get-encryption response" in caplog.text
    assert DEVICE_KEY not in caplog.text
    assert SESSION_ID not in caplog.text


# The check behind the retry, on its own: it is all that stands between an
# unverified handshake and the request.

WILDCARD = x509.DNSName("*.cas.test")


def test_expired_chain_matches_a_wildcard_name(pki):
    cas._verify_expired_cas_certificate_chain(
        _der(pki.leaf(EXPIRED, WILDCARD), pki.root), host="eu.cas.test"
    )


def test_expired_chain_for_another_host(pki):
    with pytest.raises(PyEzvizError, match="not valid for"):
        cas._verify_expired_cas_certificate_chain(
            _der(pki.leaf(EXPIRED, WILDCARD)), host="eu.other.test"
        )


def test_expired_chain_from_an_unknown_ca(pki):
    other = Pki()
    with pytest.raises(PyEzvizError, match="not valid for"):
        cas._verify_expired_cas_certificate_chain(
            _der(other.leaf(EXPIRED, WILDCARD), other.root), host="eu.cas.test"
        )


def test_expired_chain_with_a_forged_signature(pki):
    forged = pki.leaf(EXPIRED, WILDCARD, signed_by=ec.generate_private_key(ec.SECP256R1()))
    with pytest.raises(PyEzvizError, match="not valid for"):
        cas._verify_expired_cas_certificate_chain(_der(forged), host="eu.cas.test")


def test_retry_is_only_for_expired_certificates(pki):
    with pytest.raises(PyEzvizError, match="non-expired"):
        cas._verify_expired_cas_certificate_chain(
            _der(pki.leaf(VALID, WILDCARD)), host="eu.cas.test"
        )


def test_retry_needs_a_certificate():
    with pytest.raises(PyEzvizError, match="did not present"):
        cas._verify_expired_cas_certificate_chain([], host="eu.cas.test")


def test_bundle_anchors_the_cas_endpoints():
    """The shipped bundle is what an expired CAS certificate is verified against."""
    names = [
        cert.subject.rfc4514_string()
        for cert in x509.load_pem_x509_certificates(cas.CAS_CA_BUNDLE.read_bytes())
    ]
    assert any("Entrust Root Certification Authority - G2" in name for name in names)
    assert any("GlobalSign Root CA - R3" in name for name in names)
