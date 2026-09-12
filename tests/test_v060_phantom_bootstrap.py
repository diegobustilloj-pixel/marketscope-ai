from __future__ import annotations

import unittest
from pathlib import Path

import v060_phantom_bootstrap as bootstrap


class V060PhantomBootstrapTests(unittest.TestCase):
    def test_default_port_is_unique(self) -> None:
        self.assertEqual(bootstrap.main.__module__, "v060_phantom_bootstrap")
        self.assertIn("default=8770", Path(bootstrap.__file__).read_text(encoding="utf-8"))

    def test_html_names_v060_and_never_claims_start_after_preflight_only(self) -> None:
        html = bootstrap._html("csrf-test").decode("utf-8")
        self.assertIn("V0.60", html)
        self.assertNotIn("V0.56", html)
        self.assertIn("ClobAuthDomain", html)
        self.assertIn("eth_signTypedData_v4", html)
        self.assertIn("PREPARANDO: preflight PASS", html)
        self.assertIn("CAPTURE_RUNNING", html)
        self.assertIn("base creada y captura V0.60 iniciada", html)
        self.assertNotIn("preflight PASS y V0.60 iniciado", html)

    def test_environment_is_safe_type_two_without_private_key(self) -> None:
        environment = bootstrap._v060_environment(
            credentials={"api_key": "k", "api_secret": "s", "api_passphrase": "p"},
            signer_address="0x" + "1" * 40,
            maker_address="0x" + "2" * 40,
        )
        self.assertEqual(environment["PM_RFQ_SIGNATURE_TYPE"], "2")
        self.assertNotIn("PRIVATE_KEY", environment)


if __name__ == "__main__":
    unittest.main()
