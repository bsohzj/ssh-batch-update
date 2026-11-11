import socket
import unittest
from unittest.mock import Mock, patch

import paramiko

from terminal_ssh_settings import (
    SSHAlgorithms, _RestrictedTransport, restricted_connect_handler,
    supported_algorithms, test_tcp_port as tcp_check, validate_algorithms,
)


class SSHSettingsTests(unittest.TestCase):
    def test_discovery_uses_enabled_options_without_starting_ssh(self):
        with patch.object(paramiko.Transport, "start_client") as start:
            algorithms = supported_algorithms()
        start.assert_not_called()
        self.assertEqual(algorithms["kex"], paramiko.Transport._preferred_kex)
        self.assertNotIn("diffie-hellman-group1-sha1", algorithms["kex"])

    def transport(self, choices):
        left, right = socket.socketpair()
        transport = _RestrictedTransport(left, choices)
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        self.addCleanup(transport.close)
        return transport

    def test_manual_categories_exact_and_no_global_mutation(self):
        defaults = supported_algorithms()
        choices = SSHAlgorithms(defaults["kex"][0], defaults["host_key"][0],
                                "aes128-ctr", defaults["mac"][0])
        transport = self.transport(choices)
        self.assertEqual(transport.preferred_kex, (choices.kex,))
        self.assertEqual(transport.preferred_keys, (choices.host_key,))
        self.assertEqual(transport.preferred_ciphers, (choices.cipher,))
        self.assertEqual(transport.preferred_macs, (choices.mac,))
        self.assertEqual(transport.preferred_pubkeys, paramiko.Transport._preferred_pubkeys)
        transport.get_security_options().key_types = tuple(reversed(defaults["host_key"]))
        self.assertEqual(transport.preferred_keys, (choices.host_key,))
        self.assertEqual(supported_algorithms(), defaults)

    def test_manual_mac_filters_aead_and_preserves_other_categories(self):
        defaults = supported_algorithms()
        transport = self.transport(SSHAlgorithms(mac=defaults["mac"][0]))
        expected = tuple(c for c in defaults["cipher"] if not paramiko.Transport._cipher_info[c].get("is_aead"))
        self.assertEqual(transport.preferred_ciphers, expected)
        self.assertEqual(transport.get_security_options().kex, defaults["kex"])
        self.assertEqual(transport.get_security_options().key_types, defaults["host_key"])
        for cipher in defaults["cipher"]:
            if paramiko.Transport._cipher_info[cipher].get("is_aead"):
                with self.assertRaisesRegex(ValueError, "AEAD"):
                    validate_algorithms(SSHAlgorithms(cipher=cipher, mac=defaults["mac"][0]))
        with self.assertRaisesRegex(ValueError, "Unsupported SSH kex"):
            validate_algorithms(SSHAlgorithms(kex="invented"))

    def test_real_netmiko_driver_injects_instance_factory(self):
        choices = SSHAlgorithms(cipher="aes128-ctr")
        driver = restricted_connect_handler(choices, device_type="cisco_ios", host="192.0.2.1",
                                            username="example", password="example", auto_connect=False)
        client = driver._build_ssh_client()
        self.addCleanup(client.close)
        with patch.object(paramiko.SSHClient, "connect") as connect:
            client.connect("192.0.2.1", disabled_algorithms={"pubkeys": ["ssh-rsa"]})
        factory = connect.call_args.kwargs["transport_factory"]
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        transport = factory(left, disabled_algorithms={"pubkeys": ["ssh-rsa"]})
        self.addCleanup(transport.close)
        self.assertEqual(transport.preferred_ciphers, ("aes128-ctr",))
        self.assertNotIn("ssh-rsa", transport.preferred_pubkeys)


class TCPPortTests(unittest.TestCase):
    def test_success_closes_without_read_or_write(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock()
        with patch("terminal_ssh_settings.socket.create_connection", return_value=connection) as create:
            result = tcp_check("::1", 2222)
        create.assert_called_once_with(("::1", 2222), timeout=5)
        connection.__exit__.assert_called_once()
        connection.send.assert_not_called()
        connection.recv.assert_not_called()
        self.assertEqual(result.status, "success")
        self.assertGreaterEqual(result.elapsed, 0)

    def test_errors_and_validation(self):
        for error, status in ((ConnectionRefusedError("refused"), "refused"),
                              (TimeoutError("timed out"), "timeout"), (OSError("unreachable"), "error")):
            with patch("terminal_ssh_settings.socket.create_connection", side_effect=error):
                self.assertEqual(tcp_check("127.0.0.1", 22).status, status)
        with patch("terminal_ssh_settings.socket.create_connection") as create:
            for address, port in (("example.com", 22), ("127.0.0.1", 0)):
                with self.assertRaises(ValueError):
                    tcp_check(address, port)
            create.assert_not_called()
