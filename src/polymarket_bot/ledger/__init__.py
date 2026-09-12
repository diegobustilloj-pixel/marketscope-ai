"""PolyLedger P0: read-only evidence, deterministic reconstruction, no execution."""

VERSION = "polyledger-p0/0.1.0"
SAFETY = {
    "read_only": True, "real_money": False, "automatic_orders": False,
    "signing": False, "wallet_connection": False, "withdrawals": False,
}
