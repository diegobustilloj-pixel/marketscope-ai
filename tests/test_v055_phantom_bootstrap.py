from __future__ import annotations

import unittest
import urllib.error

import v055_phantom_bootstrap as bootstrap


class V055PhantomBootstrapTests(unittest.TestCase):
    def test_builds_safe_environment_without_private_key(self) -> None:
        environment = bootstrap._v055_environment(
            credentials={
                "api_key": "api-key",
                "api_secret": "api-secret",
                "api_passphrase": "api-passphrase",
            },
            signer_address="0x1111111111111111111111111111111111111111",
            maker_address="0x2222222222222222222222222222222222222222",
        )

        self.assertEqual(environment["PM_RFQ_SIGNATURE_TYPE"], "2")
        self.assertTrue(environment["PM_RFQ_SIGNER_ADDRESS"].endswith("1111"))
        self.assertTrue(environment["PM_RFQ_MAKER_ADDRESS"].endswith("2222"))
        self.assertNotIn("POLYMARKET_PRIVATE_KEY", environment)
        self.assertNotIn("PM_RFQ_PRIVATE_KEY", environment)

    def test_parses_browser_credentials_without_extra_fields(self) -> None:
        credentials = bootstrap._parse_browser_credentials(
            {
                "apiKey": "key",
                "secret": "secret",
                "passphrase": "pass",
                "ignored": "value",
            }
        )

        self.assertEqual(
            credentials,
            {"api_key": "key", "api_secret": "secret", "api_passphrase": "pass"},
        )

    def test_rejects_incomplete_browser_credentials(self) -> None:
        with self.assertRaisesRegex(bootstrap.BootstrapError, "invalidas"):
            bootstrap._parse_browser_credentials(
                {"apiKey": "key", "secret": "secret", "passphrase": ""}
            )

    def test_html_names_exact_clob_auth_contract(self) -> None:
        html = bootstrap._html("csrf-test").decode("utf-8")

        self.assertIn("ClobAuthDomain", html)
        self.assertIn("This message attests that I control the given wallet", html)
        self.assertIn("eth_signTypedData_v4", html)
        self.assertIn("chainId: 137", html)
        self.assertIn("nonce: 0", html)
        self.assertIn("csrf-test", html)
        self.assertIn("/network-check", html)
        self.assertIn("https://clob.polymarket.com/auth/api-key", html)
        self.assertIn("https://clob.polymarket.com/auth/derive-api-key", html)
        self.assertIn("fetch('/start'", html)
        self.assertNotIn("/authorize", html)
        self.assertLess(html.index("/network-check"), html.index("eth_requestAccounts"))
        self.assertLess(
            html.index("eth_signTypedData_v4"),
            html.index("credentials = await requestCredentials"),
        )

    def test_network_error_identifies_windows_socket_block(self) -> None:
        error = urllib.error.URLError(PermissionError(10013, "blocked"))

        self.assertIn("WinError 10013", bootstrap._network_error_message(error))


if __name__ == "__main__":
    unittest.main()
