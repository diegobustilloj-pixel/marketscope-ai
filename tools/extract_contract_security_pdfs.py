from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path

from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "data" / "github_forensics_20260911"
PDF_ROOT = RESEARCH / "repos" / "contract-security" / "audit-reports"
TEXT_ROOT = RESEARCH / "contract_security_text"


def safe_name(path: Path) -> str:
    relative = path.relative_to(PDF_ROOT).with_suffix("")
    value = "__".join(relative.parts)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") + ".txt"


def count_pattern(text: str, pattern: str) -> int:
    return len(re.findall(pattern, text, re.I))


def main() -> None:
    TEXT_ROOT.mkdir(parents=True, exist_ok=True)
    inventory: list[dict[str, object]] = []
    page_index: list[dict[str, object]] = []

    for pdf in sorted(PDF_ROOT.rglob("*.pdf")):
        reader = PdfReader(str(pdf))
        output_parts: list[str] = []
        all_text: list[str] = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            normalized = text.replace("\x00", "").strip()
            output_parts.append(f"\n===== PAGE {page_number} =====\n{normalized}\n")
            all_text.append(normalized)
            page_index.append(
                {
                    "pdf": str(pdf.relative_to(ROOT)).replace("\\", "/"),
                    "page": page_number,
                    "chars": len(normalized),
                    "preview": re.sub(r"\s+", " ", normalized)[:280],
                }
            )

        joined = "\n".join(all_text)
        text_path = TEXT_ROOT / safe_name(pdf)
        text_path.write_text("".join(output_parts), encoding="utf-8")
        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
        metadata = reader.metadata or {}
        path_text = str(pdf.relative_to(PDF_ROOT)).replace("\\", "/")
        inventory.append(
            {
                "path": path_text,
                "category": pdf.parent.name,
                "pages": len(reader.pages),
                "size_bytes": pdf.stat().st_size,
                "sha256": digest,
                "title_metadata": metadata.get("/Title", ""),
                "author_metadata": metadata.get("/Author", ""),
                "creation_date_metadata": metadata.get("/CreationDate", ""),
                "text_file": str(text_path.relative_to(ROOT)).replace("\\", "/"),
                "text_chars": len(joined),
                "critical_mentions": count_pattern(joined, r"\bcritical\b"),
                "high_mentions": count_pattern(joined, r"\bhigh(?: severity)?\b"),
                "medium_mentions": count_pattern(joined, r"\bmedium(?: severity)?\b"),
                "low_mentions": count_pattern(joined, r"\blow(?: severity)?\b"),
                "resolved_mentions": count_pattern(joined, r"\b(?:resolved|fixed|remediated)\b"),
                "commit_hash_mentions": count_pattern(joined, r"\b[0-9a-f]{7,40}\b"),
                "current_classification": "LEGACY" if "v1-contracts" in path_text else "CURRENT REVIEW SET (2026)",
                "date_accessed": "2026-09-11",
            }
        )

    with (RESEARCH / "contract_security_pdf_inventory.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(inventory[0]))
        writer.writeheader()
        writer.writerows(inventory)

    (RESEARCH / "contract_security_pdf_inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (RESEARCH / "contract_security_pdf_page_index.json").write_text(
        json.dumps(page_index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "pdfs": len(inventory),
                "pages": sum(int(row["pages"]) for row in inventory),
                "text_chars": sum(int(row["text_chars"]) for row in inventory),
                "output": str(RESEARCH),
            }
        )
    )


if __name__ == "__main__":
    main()
