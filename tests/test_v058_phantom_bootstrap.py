from __future__ import annotations

import unittest

import v058_phantom_bootstrap as bootstrap


class V058PhantomBootstrapTests(unittest.TestCase):
    def test_environment_is_safe_type_two_without_private_key(self) -> None:
        environment = bootstrap._v058_environment(
            credentials={"api_key": "key", "api_secret": "secret", "api_passphrase": "pass"},
            signer_address="0x1111111111111111111111111111111111111111",
            maker_address="0x2222222222222222222222222222222222222222",
        )
        self.assertEqual(environment["PM_RFQ_SIGNATURE_TYPE"], "2")
        self.assertNotIn("PM_RFQ_PRIVATE_KEY", environment)

    def test_html_names_v058_and_preserves_clob_auth(self) -> None:
        html = bootstrap._html("csrf-v058").decode("utf-8")
        self.assertIn("V0.58", html)
        self.assertNotIn("V0.56", html)
        self.assertIn("ClobAuthDomain", html)
        self.assertIn("position-to-token", html)

    def test_default_port_is_unique(self) -> None:
        source = bootstrap.Path(bootstrap.__file__).read_text(encoding="utf-8")
        self.assertIn("default=8768", source)


if __name__ == "__main__":
    unittest.main()
