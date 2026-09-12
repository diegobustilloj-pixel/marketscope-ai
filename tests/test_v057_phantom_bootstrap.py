from __future__ import annotations

import unittest

import v057_phantom_bootstrap as bootstrap


class V057PhantomBootstrapTests(unittest.TestCase):
    def test_reuses_safe_type_two_environment_without_private_key(self) -> None:
        environment = bootstrap._v057_environment(
            credentials={"api_key": "key", "api_secret": "secret", "api_passphrase": "pass"},
            signer_address="0x1111111111111111111111111111111111111111",
            maker_address="0x2222222222222222222222222222222222222222",
        )
        self.assertEqual(environment["PM_RFQ_SIGNATURE_TYPE"], "2")
        self.assertNotIn("PM_RFQ_PRIVATE_KEY", environment)
        self.assertNotIn("POLYMARKET_PRIVATE_KEY", environment)

    def test_html_names_v057_and_keeps_clob_auth(self) -> None:
        html = bootstrap._html("csrf-v057").decode("utf-8")
        self.assertIn("V0.57", html)
        self.assertNotIn("V0.56", html)
        self.assertIn("ClobAuthDomain", html)
        self.assertIn("eth_signTypedData_v4", html)
        self.assertIn("no envía cotizaciones", html)

    def test_default_port_does_not_collide(self) -> None:
        source = bootstrap.Path(bootstrap.__file__).read_text(encoding="utf-8")
        self.assertIn("default=8767", source)


if __name__ == "__main__":
    unittest.main()
