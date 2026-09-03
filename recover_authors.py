#!/usr/bin/env python3
"""Script usa-e-getta per recuperare i veri autori da export XML WordPress."""

import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

from step1_extract import NAMESPACES, get_text, open_sanitized_xml

DB_FILE = "galileo_archive.db"
XML_FILES = [
    "export_galileo-1.xml",
    "export_galileo-2.xml",
    "export_galileo-3.xml",
]
TARGET_HASH = "LAl9mX1fUl"

UPDATE_SQL = """
UPDATE articles
SET author = ?
WHERE wp_id = ?
  AND author = ?
"""


def extract_alternative_author(item: ET.Element) -> str:
    """Cerca alternative_author nei wp:postmeta dell'item."""
    for postmeta in item.findall("wp:postmeta", NAMESPACES):
        if get_text(postmeta, "wp:meta_key") != "alternative_author":
            continue
        return get_text(postmeta, "wp:meta_value")
    return ""


def parse_xml_file(xml_path: Path) -> list[tuple[str, int, str]]:
    """Estrae (vero_nome, post_id, target_hash) da un singolo export XML."""
    print(f"Lettura file: {xml_path}")
    tree = ET.parse(open_sanitized_xml(str(xml_path)))
    root = tree.getroot()

    records: list[tuple[str, int, str]] = []
    found_in_file = 0

    for item in root.iter("item"):
        post_id_text = get_text(item, "wp:post_id")
        if not post_id_text:
            continue

        author_name = extract_alternative_author(item)
        if not author_name:
            continue

        records.append((author_name, int(post_id_text), TARGET_HASH))
        found_in_file += 1

    print(f"  -> {found_in_file} autori alternative_author trovati in {xml_path.name}")
    return records


def collect_author_records(xml_files: list[str]) -> list[tuple[str, int, str]]:
    """Parsa tutti gli XML e deduplica per wp_id (ultimo file vince)."""
    by_post_id: dict[int, tuple[str, int, str]] = {}

    for xml_file in xml_files:
        xml_path = Path(xml_file)
        if not xml_path.exists():
            print(f"ATTENZIONE: file non trovato, salto: {xml_path}")
            continue

        for record in parse_xml_file(xml_path):
            _, post_id, _ = record
            by_post_id[post_id] = record

    records = list(by_post_id.values())
    print(f"\nTotale autori estratti (deduplicati per wp_id): {len(records)}")
    return records


def update_authors_in_db(
    db_path: str, records: list[tuple[str, int, str]]
) -> int:
    """Aggiorna massivamente il campo author solo per righe con hash target."""
    if not records:
        print("Nessun record da aggiornare.")
        return 0

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    changes_before = conn.total_changes

    cursor.executemany(UPDATE_SQL, records)
    conn.commit()

    total_updated = conn.total_changes - changes_before
    conn.close()
    return total_updated


def main() -> None:
    print("--- RECUPERO AUTORI DA EXPORT WORDPRESS ---")
    print(f"Database: {DB_FILE}")
    print(f"Target hash autore da sostituire: {TARGET_HASH}\n")

    records = collect_author_records(XML_FILES)
    total_updated = update_authors_in_db(DB_FILE, records)

    print("\n--- REPORT ---")
    print(f"Righe aggiornate nel database: {total_updated}")
    print("Operazione completata.")


if __name__ == "__main__":
    main()
