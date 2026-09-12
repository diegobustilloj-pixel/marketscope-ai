from polymarket_bot.car_forensics import main


if __name__ == "__main__":
    main(
        [
            "--wallet", "0x4f1d5ae26fc31472966e951af3183308736d8de2",
            "--username", "e46m3",
            "--profile", "https://polymarket.com/@e46m3",
            "--supplied-profile", "https://polymarket.com/@e46m3?tab=positions&r=Pedropica#3ZxtI6R",
            "--artifact-prefix", "e46m3",
            "--ledger", "data/polyledger/e46m3.db",
            "--metadata", "data/e46m3_forensics/e46m3_metadata.db",
            "--onchain", "data/e46m3_forensics/e46m3_onchain.db",
            "--accounting", "data/polyledger/accounting_snapshot.zip",
            "--output-dir", "data/e46m3_forensics",
        ]
    )
