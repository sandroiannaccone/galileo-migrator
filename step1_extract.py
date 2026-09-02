#!/usr/bin/env python3
"""Estrae articoli pubblicati da export XML WordPress e li salva in SQLite."""

import glob
import io
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime

XML_PATTERN = "export_galileo*.xml"
DB_FILE = "galileo_archive.db"

NAMESPACES = {
    "wp": "http://wordpress.org/export/1.2/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "excerpt": "http://wordpress.org/export/1.2/excerpt/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS articles (
    wp_id           INTEGER PRIMARY KEY,
    title           TEXT,
    slug            TEXT,
    pub_date        DATETIME,
    content         TEXT,
    excerpt         TEXT,
    author          TEXT,
    categories      TEXT,
    tags            TEXT,
    seo_title       TEXT,
    seo_description TEXT,
    seo_keyword     TEXT,
    thumbnail_id    TEXT,
    destination     TEXT,
    export_status   TEXT DEFAULT 'in_attesa'
)
"""

INSERT_SQL = """
INSERT OR REPLACE INTO articles (
    wp_id, title, slug, pub_date, content, excerpt, author,
    categories, tags, seo_title, seo_description, seo_keyword,
    thumbnail_id, destination, export_status
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

SEO_META_KEYS = {
    "_yoast_wpseo_title": "seo_title",
    "_yoast_wpseo_metadesc": "seo_description",
    "_yoast_wpseo_focuskw": "seo_keyword",
    "_thumbnail_id": "thumbnail_id",
}


def _is_valid_xml_char(char: str) -> bool:
    code = ord(char)
    return (
        char in "\t\n\r"
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
    )


def repair_corrupted_xml(content: str) -> str:
    """Ripara export troncati con HTML di errore WordPress in coda."""
    marker = "<!DOCTYPE html>"
    if marker in content:
        pos = content.index(marker)
        # Chiude l'ultimo postmeta/item lasciati aperti dall'export interrotto
        content = (
            content[:pos].rstrip()
            + "</wp:meta_value></wp:postmeta></item></channel></rss>"
        )
        print("  ATTENZIONE: rilevata corruzione in coda al file XML, tail riparato.")
    return content


def open_sanitized_xml(path: str) -> io.StringIO:
    """Rimuove caratteri non validi per XML 1.0 e ripara export corrotti."""
    with open(path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    content = "".join(c for c in content if _is_valid_xml_char(c))
    content = repair_corrupted_xml(content)
    return io.StringIO(content)


def get_text(element, tag: str) -> str:
    """Restituisce il testo di un sotto-elemento con namespace, o stringa vuota."""
    child = element.find(tag, NAMESPACES)
    if child is not None and child.text:
        return child.text.strip()
    return ""


def extract_taxonomies(item) -> tuple[str, str]:
    """Estrae categorie e tag dai nodi <category>."""
    categories = []
    tags = []
    for cat in item.findall("category"):
        domain = cat.attrib.get("domain", "")
        name = (cat.text or "").strip()
        if not name:
            continue
        if domain == "category":
            categories.append(name)
        elif domain == "post_tag":
            tags.append(name)
    return ", ".join(categories), ", ".join(tags)


def extract_postmeta(item) -> dict[str, str]:
    """Estrae i campi SEO e thumbnail dai nodi <wp:postmeta>."""
    meta = {field: "" for field in SEO_META_KEYS.values()}
    for postmeta in item.findall("wp:postmeta", NAMESPACES):
        meta_key = get_text(postmeta, "wp:meta_key")
        if meta_key not in SEO_META_KEYS:
            continue
        field = SEO_META_KEYS[meta_key]
        meta[field] = get_text(postmeta, "wp:meta_value")
    return meta


def parse_pub_date(date_str: str) -> datetime | None:
    """Converte wp:post_date in datetime."""
    if not date_str or date_str.startswith("0000"):
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def destination_for_year(year: int) -> str:
    """Assegna la destinazione in base all'anno di pubblicazione."""
    return "sanity" if year >= 2025 else "mdx"


def create_database(conn: sqlite3.Connection) -> None:
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()


def process_xml_file(
    xml_path: str,
    conn: sqlite3.Connection,
    totale_letti_da_xml: list[int],
    totale_saltati: list[int],
) -> None:
    """Parsa un singolo file XML e inserisce gli articoli validi nel database."""
    print(f"Parsing XML: {xml_path}")
    tree = ET.parse(open_sanitized_xml(xml_path))
    root = tree.getroot()

    file_letti = 0

    for item in root.iter("item"):
        post_type = get_text(item, "wp:post_type")
        status = get_text(item, "wp:status")
        if post_type != "post" or status != "publish":
            totale_saltati[0] += 1
            continue

        wp_id_text = get_text(item, "wp:post_id")
        if not wp_id_text:
            continue
        wp_id = int(wp_id_text)

        pub_date_raw = get_text(item, "wp:post_date")
        pub_dt = parse_pub_date(pub_date_raw)
        pub_date = pub_dt.strftime("%Y-%m-%d %H:%M:%S") if pub_dt else pub_date_raw

        year = pub_dt.year if pub_dt else 0
        destination = destination_for_year(year)

        categories, tags = extract_taxonomies(item)
        meta = extract_postmeta(item)

        conn.execute(
            INSERT_SQL,
            (
                wp_id,
                get_text(item, "title"),
                get_text(item, "wp:post_name"),
                pub_date,
                get_text(item, "content:encoded"),
                get_text(item, "excerpt:encoded"),
                get_text(item, "dc:creator"),
                categories,
                tags,
                meta["seo_title"],
                meta["seo_description"],
                meta["seo_keyword"],
                meta["thumbnail_id"],
                destination,
                "in_attesa",
            ),
        )

        totale_letti_da_xml[0] += 1
        file_letti += 1

        if totale_letti_da_xml[0] % 1000 == 0:
            conn.commit()
            print(f"  ... {totale_letti_da_xml[0]} articoli elaborati (totale globale)")

    conn.commit()
    print(f"  -> {file_letti} articoli validi da questo file")


def run_db_sanity_checks(conn: sqlite3.Connection) -> tuple[int, dict[str, int]]:
    """Esegue i controlli di integrità finali sul database."""
    unique_count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    breakdown_rows = conn.execute(
        "SELECT destination, COUNT(*) FROM articles GROUP BY destination"
    ).fetchall()
    breakdown = {row[0]: row[1] for row in breakdown_rows}
    return unique_count, breakdown


def print_report(
    files_processed: list[str],
    totale_letti: int,
    totale_saltati: int,
    unique_count: int,
    breakdown: dict[str, int],
) -> None:
    """Stampa il report finale di estrazione e integrità."""
    print()
    print("--- REPORT DI ESTRAZIONE ---")
    print(f"File processati: {len(files_processed)}")
    for f in files_processed:
        print(f"  - {f}")
    print(f"Articoli totali letti dagli XML: {totale_letti}")
    print(f"Nodi <item> saltati (non post pubblicati): {totale_saltati}")
    print()
    print("--- CONTROLLO INTEGRITÀ DATABASE ---")
    diff_note = ""
    if totale_letti != unique_count:
        diff = totale_letti - unique_count
        diff_note = (
            f" (differenza: {diff} — duplicati sovrascritti o righe aggiornate)"
        )
    print(f"Articoli UNIVOCI salvati nel DB: {unique_count}{diff_note}")
    print()
    print("--- BREAKDOWN ---")
    print(f"MDX (Archivio <= 2024): {breakdown.get('mdx', 0)}")
    print(f"Sanity (Nuovi >= 2025): {breakdown.get('sanity', 0)}")
    print(f"Database salvato in: {DB_FILE}")


def extract_articles(db_path: str = DB_FILE) -> None:
    """Trova tutti gli export XML, li elabora e stampa il report finale."""
    xml_files = sorted(glob.glob(XML_PATTERN))
    if not xml_files:
        print(f"Nessun file trovato con pattern '{XML_PATTERN}'")
        return

    totale_letti_da_xml = [0]
    totale_saltati = [0]

    conn = sqlite3.connect(db_path)
    create_database(conn)

    for xml_path in xml_files:
        process_xml_file(xml_path, conn, totale_letti_da_xml, totale_saltati)

    unique_count, breakdown = run_db_sanity_checks(conn)
    conn.close()

    print_report(
        xml_files,
        totale_letti_da_xml[0],
        totale_saltati[0],
        unique_count,
        breakdown,
    )


if __name__ == "__main__":
    extract_articles()
