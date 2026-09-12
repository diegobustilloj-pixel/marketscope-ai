from __future__ import annotations

import sys

from polymarket_bot.car_shadow_tracker import main


if __name__ == "__main__":
    main(
        [
            "--wallet", "0x4f1d5ae26fc31472966e951af3183308736d8de2",
            "--database", "data/e46m3_forensics/e46m3_shadow.db",
            *sys.argv[1:],
        ]
    )
