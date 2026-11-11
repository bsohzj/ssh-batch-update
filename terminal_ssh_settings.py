"""Per-connection SSH negotiation preferences and a TCP-only reachability check."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import paramiko


@dataclass(frozen=True)
class SSHAlgorithms:
    kex: str | None = None
    host_key: str | None = None
    cipher: str | None = None
    mac: str | None = None

    @property
    def automatic(self) -> bool:
        return not any((self.kex, self.host_key, self.cipher, self.mac))


def supported_algorithms() -> dict[str, tuple[str, ...]]:
    """Inspect enabled preferences without starting SSH or opening a network port."""
    left, right = socket.socketpair()
    try:
        transport = paramiko.Transport(left)
        try:
            options = transport.get_security_options()
            return {
                "kex": options.kex, "host_key": options.key_types,
                "cipher": options.ciphers, "mac": options.digests,
            }
        finally:
            transport.close()
    finally:
        left.close()
        right.close()


def _is_aead(cipher: str) -> bool:
    return bool(paramiko.Transport._cipher_info[cipher].get("is_aead"))


def validate_algorithms(choices: SSHAlgorithms) -> None:
    for category, eligible in supported_algorithms().items():
        choice = getattr(choices, category)
        if choice is not None and choice not in eligible:
            raise ValueError(f"Unsupported SSH {category} algorithm: {choice}")
    if choices.mac and choices.cipher and _is_aead(choices.cipher):
        raise ValueError("An explicit MAC cannot be used with an AEAD cipher. Choose Automatic MAC or a non-AEAD cipher.")


class _RestrictedTransport(paramiko.Transport):
    def __init__(self, sock: Any, choices: SSHAlgorithms, **kwargs: Any) -> None:
        self._exact_host_key = choices.host_key
        super().__init__(sock, **kwargs)
        options = self.get_security_options()
        for category, attribute in (
            ("kex", "kex"), ("host_key", "key_types"),
            ("cipher", "ciphers"), ("mac", "digests"),
        ):
            choice = getattr(choices, category)
            if choice:
                setattr(options, attribute, (choice,))
        if choices.mac and not choices.cipher:
            options.ciphers = tuple(cipher for cipher in options.ciphers if not _is_aead(cipher))
            if not options.ciphers:
                raise ValueError("No non-AEAD cipher is available for the selected MAC.")

    @property
    def preferred_keys(self) -> tuple[str, ...]:
        # Paramiko normally adds certificate variants implicitly. A manual choice is exact.
        if self._exact_host_key:
            return (self._exact_host_key,)
        return super().preferred_keys


def restricted_connect_handler(choices: SSHAlgorithms, **parameters: Any) -> Any:
    """Customize only this Netmiko instance; leave automatic connections untouched."""
    from netmiko import ssh_dispatcher

    validate_algorithms(choices)

    class RestrictedClient(paramiko.SSHClient):
        def connect(self, *args: Any, **kwargs: Any) -> Any:
            kwargs["transport_factory"] = lambda sock, **options: _RestrictedTransport(
                sock, choices, **options
            )
            return super().connect(*args, **kwargs)

    driver = ssh_dispatcher(parameters["device_type"])

    class RestrictedDriver(driver):
        def _get_ssh_client_instance(self) -> paramiko.SSHClient:
            return RestrictedClient()

    return RestrictedDriver(**parameters)


@dataclass(frozen=True)
class TCPPortResult:
    status: str
    elapsed: float
    detail: str = ""


def test_tcp_port(address: str, port: int) -> TCPPortResult:
    """Open and close TCP only: no authentication, SSH handshake, reads or writes."""
    address = str(ipaddress.ip_address(address.strip()))
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    start = perf_counter()
    try:
        with socket.create_connection((address, port), timeout=5):
            pass
    except ConnectionRefusedError as exc:
        return TCPPortResult("refused", perf_counter() - start, str(exc))
    except TimeoutError as exc:
        return TCPPortResult("timeout", perf_counter() - start, str(exc))
    except OSError as exc:
        return TCPPortResult("error", perf_counter() - start, str(exc))
    return TCPPortResult("success", perf_counter() - start)
