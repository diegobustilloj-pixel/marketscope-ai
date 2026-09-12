from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import tempfile
import zipfile
import zlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("export-v3")
V3_CODEC = "zlib-jsonl-v1"
MAX_SAMPLE_PAYLOAD_CHARS = 100_000
MAX_CHUNKS_TO_SAMPLE = 20_000
PRIORITY_STREAMS = {
    ("clob", "book"),
    ("clob", "best_bid_ask"),
    ("clob", "price_change"),
    ("clob", "last_trade_price"),
    ("clob", "new_market"),
    ("gamma", "events_metadata"),
    ("rtds", "crypto_prices"),
    ("rtds", "crypto_prices_chainlink"),
}


def _read_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _small_sample(item: dict[str, Any]) -> dict[str, Any]:
    sample = dict(item)
    payload = sample.get("payload_raw")
    if isinstance(payload, str) and len(payload) > MAX_SAMPLE_PAYLOAD_CHARS:
        sample["payload_raw"] = payload[:MAX_SAMPLE_PAYLOAD_CHARS]
        sample["payload_truncated"] = True
        sample["payload_original_chars"] = len(payload)
    return sample


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _zip_directory(source: Path, output: Path) -> None:
    with zipfile.ZipFile(
        output, mode="x", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in sorted(source.iterdir()):
            archive.write(path, arcname=path.name)


def export_v3_dataset(
    *,
    source_db: str | Path,
    output_zip: str | Path,
    samples_per_stream: int = 5,
) -> dict[str, Any]:
    """Exporta metadatos y ejemplos pequeños sin modificar la base V3."""
    if samples_per_stream <= 0 or samples_per_stream > 100:
        raise ValueError("--samples-per-stream debe estar entre 1 y 100")

    source = Path(source_db).expanduser().resolve()
    output = Path(output_zip).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"No se encontró la base V3: {source}")
    if output.exists():
        raise ValueError(
            f"El archivo de salida ya existe; muévalo o elimínelo: {output}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        f"{source.as_uri()}?mode=ro", uri=True, timeout=60
    )
    connection.row_factory = sqlite3.Row
    try:
        LOGGER.info("Base V3 abierta en solo lectura: %s", source)
        LOGGER.info("Comprobando la estructura SQLite; puede tardar.")
        schema_row = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        schema_version = str(schema_row["value"]) if schema_row else None
        if schema_version != "3":
            raise ValueError(
                "La exportación requiere una base de captura V3; "
                f"se encontró schema_version={schema_version!r}."
            )

        quick_check = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        )
        LOGGER.info("SQLite quick_check: %s", quick_check)
        counts_rows = connection.execute(
            """
            SELECT source, stream, event_count
            FROM event_counts
            ORDER BY source, stream
            """
        ).fetchall()
        by_source = [
            {
                "source": str(row["source"]),
                "stream": str(row["stream"]),
                "count": int(row["event_count"]),
            }
            for row in counts_rows
        ]
        available = {
            (item["source"], item["stream"]) for item in by_source
        }
        targets = PRIORITY_STREAMS & available
        bounds = connection.execute(
            """
            SELECT COUNT(*) AS chunks,
                   COALESCE(SUM(event_count), 0) AS events,
                   COALESCE(SUM(raw_bytes), 0) AS raw_bytes,
                   COALESCE(SUM(compressed_bytes), 0) AS compressed_bytes,
                   MIN(first_received_at) AS first_received_at,
                   MAX(last_received_at) AS last_received_at
            FROM raw_chunks
            """
        ).fetchone()
        market_count = int(
            connection.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        )

        samples: list[dict[str, Any]] = []
        sample_counts: Counter[tuple[str, str]] = Counter()
        chunks_scanned = 0
        unreadable_chunks = 0
        rows = connection.execute(
            """
            SELECT codec, payload_blob
            FROM raw_chunks
            ORDER BY first_sequence
            LIMIT ?
            """,
            (MAX_CHUNKS_TO_SAMPLE,),
        )
        LOGGER.info("Extrayendo muestras de %d streams.", len(targets))
        for row in rows:
            chunks_scanned += 1
            try:
                if row["codec"] != V3_CODEC:
                    raise ValueError(f"codec inesperado: {row['codec']}")
                raw = zlib.decompress(row["payload_blob"])
            except (ValueError, zlib.error):
                unreadable_chunks += 1
                continue
            for line in raw.splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pair = (str(item.get("source")), str(item.get("stream")))
                if (
                    pair in targets
                    and sample_counts[pair] < samples_per_stream
                ):
                    samples.append(_small_sample(item))
                    sample_counts[pair] += 1
            if targets and all(
                sample_counts[pair] >= samples_per_stream
                for pair in targets
            ):
                break
        LOGGER.info(
            "Muestras listas: %d eventos desde %d bloques.",
            len(samples),
            chunks_scanned,
        )

        raw_bytes = int(bounds["raw_bytes"])
        compressed_bytes = int(bounds["compressed_bytes"])
        manifest = {
            "export_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "source_database": {
                "filename": source.name,
                "size_bytes": source.stat().st_size,
                "schema_version": int(schema_version),
                "sqlite_quick_check": quick_check,
                "chunks": int(bounds["chunks"]),
                "events": int(bounds["events"]),
                "markets": market_count,
                "first_received_at": bounds["first_received_at"],
                "last_received_at": bounds["last_received_at"],
                "raw_bytes": raw_bytes,
                "compressed_bytes": compressed_bytes,
                "compression_ratio": (
                    round(raw_bytes / compressed_bytes, 3)
                    if compressed_bytes
                    else None
                ),
                "by_source": by_source,
            },
            "sample": {
                "requested_per_stream": samples_per_stream,
                "targets": [
                    {"source": pair[0], "stream": pair[1]}
                    for pair in sorted(targets)
                ],
                "captured": [
                    {
                        "source": pair[0],
                        "stream": pair[1],
                        "count": sample_counts[pair],
                    }
                    for pair in sorted(targets)
                ],
                "chunks_scanned": chunks_scanned,
                "unreadable_chunks": unreadable_chunks,
                "payload_character_limit": MAX_SAMPLE_PAYLOAD_CHARS,
            },
            "notes": [
                "La base original se abrió en modo de solo lectura.",
                "Este ZIP contiene metadatos y muestras, no los eventos completos.",
                "Conserve la base V3 hasta terminar el análisis y los respaldos.",
            ],
        }

        with tempfile.TemporaryDirectory(
            prefix="polymarket-export-", dir=output.parent
        ) as temporary:
            export_dir = Path(temporary)
            _write_json(export_dir / "manifest.json", manifest)
            with (export_dir / "markets.jsonl").open(
                "w", encoding="utf-8", newline="\n"
            ) as handle:
                for market in connection.execute(
                    """
                    SELECT condition_id, slug, event_id, question,
                           start_at, end_at, resolution_source,
                           outcomes_json, token_ids_json,
                           active, closed, accepting_orders,
                           first_seen_at, last_seen_at
                    FROM markets
                    ORDER BY start_at, slug
                    """
                ):
                    item = dict(market)
                    item["outcomes"] = _read_json(
                        str(item.pop("outcomes_json"))
                    )
                    item["token_ids"] = _read_json(
                        str(item.pop("token_ids_json"))
                    )
                    handle.write(
                        json.dumps(
                            item,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
            with (export_dir / "samples.jsonl").open(
                "w", encoding="utf-8", newline="\n"
            ) as handle:
                for sample in samples:
                    handle.write(
                        json.dumps(
                            sample,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
            _zip_directory(export_dir, output)
            LOGGER.info("ZIP creado: %s", output)
    finally:
        connection.close()

    digest = hashlib.sha256()
    with output.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "ok": True,
        "source_database": str(source),
        "source_database_unchanged": True,
        "output_zip": str(output),
        "output_bytes": output.stat().st_size,
        "output_sha256": digest.hexdigest(),
        "events_in_original": int(bounds["events"]),
        "markets_exported": market_count,
        "samples_exported": len(samples),
        "chunks_scanned_for_samples": chunks_scanned,
    }
