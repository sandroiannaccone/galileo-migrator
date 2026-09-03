#!/usr/bin/env python3
"""
Classificazione automatica degli articoli "irriducibili" tramite Google Gemini.

Prerequisiti:
    pip install google-generativeai python-dotenv tqdm

Configurazione:
    Crea un file .env nella root del progetto con:
    GEMINI_API_KEY=la_tua_chiave_api
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

try:
    import google.generativeai as genai
    from dotenv import load_dotenv
    from tqdm import tqdm
except ImportError:
    print(
        "Dipendenze mancanti. Installa i pacchetti richiesti con:\n"
        "    pip install google-generativeai python-dotenv tqdm"
    )
    sys.exit(1)

import os

DB_FILE = "galileo_archive.db"
MAX_API_RETRIES = 3
RETRY_SLEEP_SECONDS = 2

VALID_CATEGORIES = {
    "Corpo e Mente",
    "Pianeta e Animali",
    "Spazio ed Esplorazione",
    "Futuri",
    "Materia e Numeri",
    "Noi Umani",
}

SYSTEM_INSTRUCTION = """Sei un editor scientifico esperto. Devi classificare un articolo giornalistico leggendone il titolo (e l'abstract, se presente).
Ecco il significato esatto delle categorie a tua disposizione:

Corpo e Mente: medicina, salute, neuroscienze, biologia umana, psicologia, farmaci.

Pianeta e Animali: ambiente, clima, zoologia, botanica, geologia, ecologia, scienze della terra.

Spazio ed Esplorazione: astronomia, astrofisica, missioni spaziali, cosmologia.

Futuri: intelligenza artificiale, robotica, innovazione tecnologica, energia del futuro, impatto sociale della tecnologia.

Materia e Numeri: fisica, chimica, matematica, scienza dei materiali, statistica.

Noi Umani: antropologia, evoluzione, sociologia, genetica delle popolazioni, storia della scienza, archeologia.

Le UNICHE risposte valide sono i nomi esatti delle categorie qui sopra (senza asterischi o formattazione).
Se il titolo è incomprensibile, ambiguo, ironico o non permette di dedurre chiaramente l'argomento scientifico, rispondi ESATTAMENTE con la parola: MANUALE.
Restituisci solo la stringa esatta. Nessuna spiegazione, nessuna punteggiatura."""


def get_connection(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path)


def resolve_abstract_column(conn: sqlite3.Connection) -> str | None:
    """Restituisce il nome della colonna abstract/excerpt se presente in articles."""
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()
    }
    if "abstract" in columns:
        return "abstract"
    if "excerpt" in columns:
        return "excerpt"
    return None


def fetch_irriducibili(
    conn: sqlite3.Connection,
) -> list[tuple[int, str, str | None]]:
    """
    Estrae gli articoli attivi non ancora presenti in article_overrides.
    Usa wp_id (chiave primaria reale del database galileo_archive.db).
    """
    abstract_col = resolve_abstract_column(conn)
    abstract_select = (
        f"a.{abstract_col} AS abstract" if abstract_col else "NULL AS abstract"
    )

    query = f"""
        SELECT a.wp_id, a.title, {abstract_select}
        FROM articles a
        WHERE a.is_deleted = 0
          AND a.wp_id NOT IN (SELECT wp_id FROM article_overrides)
        ORDER BY a.wp_id
    """
    rows = conn.execute(query).fetchall()
    return [(wp_id, title or "", abstract) for wp_id, title, abstract in rows]


def build_user_prompt(title: str, abstract: str | None) -> str:
    """Costruisce il prompt utente con titolo e abstract (se disponibile)."""
    abstract_text = (abstract or "").strip()
    if abstract_text:
        return (
            f"Titolo: {title.strip()}\n"
            f"Abstract: {abstract_text}"
        )
    return f"Titolo: {title.strip()}"


def clean_model_response(response_text: str) -> str:
    """Normalizza la risposta del modello rimuovendo spazi e formattazione markdown."""
    return response_text.strip().strip("*").strip().strip('"').strip("'")


def classify_article(
    model: genai.GenerativeModel, title: str, abstract: str | None
) -> str | None:
    """Invoca Gemini; in caso di errore ritenta fino a 3 volte, poi restituisce None."""
    prompt = build_user_prompt(title, abstract)

    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            response = model.generate_content(prompt)
            if not response or not response.text:
                return "MANUALE"
            return clean_model_response(response.text)
        except Exception:
            if attempt < MAX_API_RETRIES:
                time.sleep(RETRY_SLEEP_SECONDS)

    return None


def save_override(conn: sqlite3.Connection, wp_id: int, target_category: str) -> None:
    """Scrive l'override di categoria nel database."""
    conn.execute(
        """
        INSERT OR REPLACE INTO article_overrides
            (wp_id, target_category, is_deleted)
        VALUES (?, ?, 0)
        """,
        (wp_id, target_category),
    )


def ensure_override_table(conn: sqlite3.Connection) -> None:
    """Garantisce l'esistenza della tabella article_overrides."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS article_overrides (
            wp_id             INTEGER PRIMARY KEY,
            target_category   TEXT,
            is_deleted        INTEGER DEFAULT 0
        )
        """
    )


def list_generate_content_models() -> list:
    """Restituisce i modelli Gemini che supportano generateContent."""
    return [
        model
        for model in genai.list_models()
        if "generateContent" in getattr(model, "supported_generation_methods", [])
    ]


def select_model_interactive() -> str:
    """Mostra un menu numerato e restituisce il nome del modello scelto."""
    modelli_disponibili = list_generate_content_models()
    if not modelli_disponibili:
        print("Nessun modello Gemini con supporto generateContent trovato.")
        sys.exit(1)

    print("\nModelli Gemini disponibili (generateContent):\n")
    for index, model in enumerate(modelli_disponibili, start=1):
        print(f"  {index}. {model.name}")

    while True:
        scelta = input("\nDigita il numero del modello da usare: ").strip()
        if scelta.isdigit():
            index = int(scelta)
            if 1 <= index <= len(modelli_disponibili):
                return modelli_disponibili[index - 1].name
        print("Selezione non valida. Inserisci un numero dall'elenco.")


def main(model_name: str) -> None:
    db_path = Path(DB_FILE)
    if not db_path.exists():
        print(f"Database non trovato: {db_path.resolve()}")
        sys.exit(1)

    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=SYSTEM_INSTRUCTION,
    )

    conn = get_connection(str(db_path))
    ensure_override_table(conn)

    articles = fetch_irriducibili(conn)
    if not articles:
        print("Nessun articolo da classificare: tutti hanno già un override.")
        conn.close()
        return

    print(f"Articoli da classificare: {len(articles)}")
    print(f"Modello: {model_name}")
    print(f"Database: {db_path.resolve()}\n")

    classified = 0
    manual = 0
    skipped = 0
    errors = 0

    for wp_id, title, abstract in tqdm(articles, desc="Classificazione Gemini", unit="art"):
        category = classify_article(model, title, abstract)
        if category is None:
            errors += 1
            tqdm.write(f"Salto wp_id {wp_id} per errore API")
            continue

        if category == "MANUALE":
            manual += 1
            continue

        if category not in VALID_CATEGORIES:
            skipped += 1
            tqdm.write(
                f"wp_id {wp_id}: risposta anomala '{category}' — lasciato per revisione manuale."
            )
            continue

        save_override(conn, wp_id, category)
        conn.commit()
        classified += 1

    conn.close()

    print("\n--- REPORT CLASSIFICAZIONE ---")
    print(f"Classificati automaticamente : {classified}")
    print(f"Inviati a revisione (MANUALE) : {manual}")
    print(f"Risposte anomale (saltate)    : {skipped}")
    print(f"Errori API irrecuperabili     : {errors}")
    print("Operazione completata.")


if __name__ == "__main__":
    load_dotenv()

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print(
            "GEMINI_API_KEY non trovata. Aggiungila al file .env:\n"
            "    GEMINI_API_KEY=la_tua_chiave_api"
        )
        sys.exit(1)

    genai.configure(api_key=api_key)
    nome_modello = select_model_interactive()
    print(f"\nModello selezionato: {nome_modello}\n")
    main(nome_modello)
