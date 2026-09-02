#!/usr/bin/env python3
"""Cleaning Room GUI per mappare categorie e unificare autori su galileo_archive.db."""

import sqlite3

import pandas as pd
import streamlit as st

DB_FILE = "galileo_archive.db"

TARGET_CATEGORIES = [
    "Spazio",
    "Medicina",
    "Fisica & Matematica",
    "Ricerca d'Italia",
    "Società",
    "Ambiente",
    "Vita",
    "Tecnologia",
]

TRASH_OPTION = "🗑️ CESTINA"
CATEGORY_SELECT_OPTIONS = [""] + TARGET_CATEGORIES + [TRASH_OPTION]

CREATE_CATEGORY_MAP_SQL = """
CREATE TABLE IF NOT EXISTS category_map (
    old_cat TEXT PRIMARY KEY,
    new_cat TEXT
)
"""

CREATE_AUTHOR_MAP_SQL = """
CREATE TABLE IF NOT EXISTS author_map (
    old_author TEXT PRIMARY KEY,
    new_author TEXT
)
"""

CREATE_ARTICLE_OVERRIDES_SQL = """
CREATE TABLE IF NOT EXISTS article_overrides (
    wp_id   INTEGER PRIMARY KEY,
    new_cat TEXT
)
"""


def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_FILE)


def init_mapping_tables(conn: sqlite3.Connection) -> None:
    conn.execute(CREATE_CATEGORY_MAP_SQL)
    conn.execute(CREATE_AUTHOR_MAP_SQL)
    conn.execute(CREATE_ARTICLE_OVERRIDES_SQL)
    try:
        conn.execute(
            "ALTER TABLE articles ADD COLUMN is_deleted INTEGER DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass
    conn.commit()


def load_category_map(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT old_cat, new_cat FROM category_map").fetchall()
    return {old: new for old, new in rows}


def load_author_map(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT old_author, new_author FROM author_map").fetchall()
    return {old: new for old, new in rows}


def article_contains_category(categories_str: str, category: str) -> bool:
    if not categories_str or pd.isna(categories_str):
        return False
    return category in [c.strip() for c in str(categories_str).split(",")]


def get_wp_ids_with_exact_category(
    conn: sqlite3.Connection, old_cat: str
) -> list[int]:
    """Trova wp_id con match esatto sulla categoria (split + strip)."""
    rows = conn.execute(
        "SELECT wp_id, categories FROM articles WHERE is_deleted = 0"
    ).fetchall()
    return [
        wp_id
        for wp_id, categories_str in rows
        if article_contains_category(categories_str, old_cat)
    ]


def soft_delete_articles(conn: sqlite3.Connection, wp_ids: list[int]) -> int:
    deleted = 0
    for wp_id in wp_ids:
        conn.execute(
            "UPDATE articles SET is_deleted = 1 WHERE wp_id = ?", (wp_id,)
        )
        deleted += 1
    return deleted


def build_category_counts_df(conn: sqlite3.Connection) -> pd.DataFrame:
    """Esplode le categorie comma-separated e conta gli articoli per categoria."""
    articles_df = pd.read_sql_query(
        """
        SELECT categories FROM articles
        WHERE is_deleted = 0
          AND categories IS NOT NULL
          AND categories != ''
        """,
        conn,
    )
    if articles_df.empty:
        return pd.DataFrame(
            columns=["Vecchia Categoria", "Conteggio Articoli", "Nuova Categoria"]
        )

    exploded = (
        articles_df.assign(category=articles_df["categories"].str.split(","))
        .explode("category")
    )
    exploded["category"] = exploded["category"].str.strip()
    exploded = exploded[exploded["category"] != ""]

    counts = exploded["category"].value_counts().reset_index()
    counts.columns = ["Vecchia Categoria", "Conteggio Articoli"]

    existing_map = load_category_map(conn)
    counts["Nuova Categoria"] = (
        counts["Vecchia Categoria"].map(existing_map).fillna("")
    )

    return counts.sort_values("Conteggio Articoli", ascending=False).reset_index(
        drop=True
    )


def get_articles_for_category(
    conn: sqlite3.Connection, category: str
) -> pd.DataFrame:
    """Filtra gli articoli attivi che contengono la categoria selezionata."""
    articles_df = pd.read_sql_query(
        """
        SELECT wp_id, title, pub_date, categories
        FROM articles
        WHERE is_deleted = 0
        """,
        conn,
    )
    mask = articles_df["categories"].apply(
        lambda x: article_contains_category(x, category)
    )
    filtered = articles_df[mask].copy()

    overrides_df = pd.read_sql_query(
        "SELECT wp_id, new_cat FROM article_overrides", conn
    )
    if not overrides_df.empty:
        filtered = filtered.merge(overrides_df, on="wp_id", how="left")
        filtered["Assegnazione Singola"] = filtered["new_cat"].fillna("")
        filtered = filtered.drop(columns=["new_cat"])
    else:
        filtered["Assegnazione Singola"] = ""

    filtered = filtered.rename(
        columns={
            "title": "Titolo",
            "pub_date": "Data",
            "categories": "Categorie Originali",
        }
    )
    return filtered[
        ["wp_id", "Titolo", "Data", "Categorie Originali", "Assegnazione Singola"]
    ]


def extract_unique_authors(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT author FROM articles
        WHERE is_deleted = 0
          AND author IS NOT NULL
          AND author != ''
        """
    ).fetchall()
    return sorted(row[0] for row in rows)


def save_category_map(conn: sqlite3.Connection, df: pd.DataFrame) -> tuple[int, int]:
    """Salva mappature o esegue soft delete per categorie cestinate."""
    mapped = 0
    deleted = 0
    for _, row in df.iterrows():
        new_cat = row.get("Nuova Categoria", "")
        if pd.isna(new_cat) or not str(new_cat).strip():
            continue

        old_cat = row["Vecchia Categoria"]
        new_cat = str(new_cat).strip()

        if new_cat == TRASH_OPTION:
            wp_ids = get_wp_ids_with_exact_category(conn, old_cat)
            deleted += soft_delete_articles(conn, wp_ids)
        elif new_cat in TARGET_CATEGORIES:
            conn.execute(
                "INSERT OR REPLACE INTO category_map (old_cat, new_cat) VALUES (?, ?)",
                (old_cat, new_cat),
            )
            mapped += 1

    conn.commit()
    return mapped, deleted


def save_author_map(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    saved = 0
    for _, row in df.iterrows():
        new_author = row.get("Nuovo Autore", "")
        if pd.isna(new_author) or not str(new_author).strip():
            continue
        conn.execute(
            "INSERT OR REPLACE INTO author_map (old_author, new_author) VALUES (?, ?)",
            (row["Vecchio Autore"], str(new_author).strip()),
        )
        saved += 1
    conn.commit()
    return saved


def save_article_overrides(conn: sqlite3.Connection, df: pd.DataFrame) -> tuple[int, int]:
    """Salva override o esegue soft delete per singoli articoli."""
    saved = 0
    deleted = 0
    for _, row in df.iterrows():
        assignment = row.get("Assegnazione Singola", "")
        if pd.isna(assignment) or not str(assignment).strip():
            continue

        wp_id = int(row["wp_id"])
        assignment = str(assignment).strip()

        if assignment == TRASH_OPTION:
            conn.execute(
                "UPDATE articles SET is_deleted = 1 WHERE wp_id = ?", (wp_id,)
            )
            deleted += 1
        elif assignment in TARGET_CATEGORIES:
            conn.execute(
                "INSERT OR REPLACE INTO article_overrides (wp_id, new_cat) VALUES (?, ?)",
                (wp_id, assignment),
            )
            saved += 1

    conn.commit()
    return saved, deleted


def render_dashboard(conn: sqlite3.Connection) -> None:
    st.header("Dashboard")

    total = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE is_deleted = 0"
    ).fetchone()[0]
    mdx = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE is_deleted = 0 AND destination = 'mdx'"
    ).fetchone()[0]
    sanity = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE is_deleted = 0 AND destination = 'sanity'"
    ).fetchone()[0]

    col1, col2, col3 = st.columns(3)
    col1.metric("Totale Articoli", total)
    col2.metric("Totale MDX", mdx)
    col3.metric("Totale Sanity", sanity)

    st.subheader("Anteprima articoli")
    preview_df = pd.read_sql_query(
        "SELECT * FROM articles WHERE is_deleted = 0 LIMIT 50", conn
    )
    st.dataframe(preview_df, use_container_width=True)


def render_category_mapping(conn: sqlite3.Connection) -> None:
    st.header("Mappatura Categorie")
    st.caption(
        "Associa ogni vecchia categoria WordPress a una delle 8 categorie target, "
        "oppure cestina gli articoli associati."
    )

    if msg := st.session_state.pop("category_save_msg", None):
        st.success(msg)

    df = build_category_counts_df(conn)

    edited_df = st.data_editor(
        df,
        column_config={
            "Vecchia Categoria": st.column_config.TextColumn(
                "Vecchia Categoria", disabled=True
            ),
            "Conteggio Articoli": st.column_config.NumberColumn(
                "Conteggio Articoli", disabled=True, format="%d"
            ),
            "Nuova Categoria": st.column_config.SelectboxColumn(
                "Nuova Categoria",
                options=CATEGORY_SELECT_OPTIONS,
                required=False,
            ),
        },
        hide_index=True,
        use_container_width=True,
        key="category_editor",
    )

    if st.button("Salva Mappatura Categorie", type="primary"):
        mapped, deleted = save_category_map(conn, edited_df)
        parts = []
        if mapped:
            parts.append(f"{mapped} mappature in category_map")
        if deleted:
            parts.append(f"{deleted} articoli cestinati")
        st.session_state["category_save_msg"] = (
            "Salvataggio completato: " + ", ".join(parts) + "."
            if parts
            else "Nessuna modifica da salvare."
        )
        st.rerun()

    st.divider()
    st.subheader("Analisi Granulare Categorie Miste")

    old_categories = df["Vecchia Categoria"].tolist()
    if not old_categories:
        st.info("Nessuna categoria trovata negli articoli.")
        return

    selected_cat = st.selectbox(
        "Seleziona una vecchia categoria da ispezionare",
        options=old_categories,
    )

    if selected_cat:
        if msg := st.session_state.pop("override_save_msg", None):
            st.success(msg)

        articles_df = get_articles_for_category(conn, selected_cat)
        st.caption(
            f"{len(articles_df)} articoli contengono la categoria «{selected_cat}»"
        )

        edited_articles = st.data_editor(
            articles_df,
            column_config={
                "wp_id": st.column_config.NumberColumn(
                    "wp_id", disabled=True, format="%d"
                ),
                "Titolo": st.column_config.TextColumn("Titolo", disabled=True),
                "Data": st.column_config.TextColumn("Data", disabled=True),
                "Categorie Originali": st.column_config.TextColumn(
                    "Categorie Originali", disabled=True
                ),
                "Assegnazione Singola": st.column_config.SelectboxColumn(
                    "Assegnazione Singola",
                    options=CATEGORY_SELECT_OPTIONS,
                    required=False,
                ),
            },
            hide_index=True,
            use_container_width=True,
            key=f"article_override_{selected_cat}",
        )

        if st.button("Salva Assegnazioni Singole", type="primary"):
            saved, deleted = save_article_overrides(conn, edited_articles)
            parts = []
            if saved:
                parts.append(f"{saved} override in article_overrides")
            if deleted:
                parts.append(f"{deleted} articoli cestinati")
            st.session_state["override_save_msg"] = (
                "Salvataggio completato: " + ", ".join(parts) + "."
                if parts
                else "Nessuna modifica da salvare."
            )
            st.rerun()


def render_author_unification(conn: sqlite3.Connection) -> None:
    st.header("Unificazione Autori")
    st.caption(
        "Unifica varianti dello stesso autore indicando il nome corretto."
    )

    existing_map = load_author_map(conn)
    old_authors = extract_unique_authors(conn)

    rows = [
        {
            "Vecchio Autore": old,
            "Nuovo Autore": existing_map.get(old, ""),
        }
        for old in old_authors
    ]
    df = pd.DataFrame(rows)

    edited_df = st.data_editor(
        df,
        column_config={
            "Vecchio Autore": st.column_config.TextColumn(
                "Vecchio Autore", disabled=True
            ),
            "Nuovo Autore": st.column_config.TextColumn("Nuovo Autore"),
        },
        hide_index=True,
        use_container_width=True,
        key="author_editor",
    )

    if st.button("Salva Unificazione Autori", type="primary"):
        saved = save_author_map(conn, edited_df)
        st.success(f"Unificazione salvata: {saved} associazioni scritte in author_map.")


def main() -> None:
    st.set_page_config(
        page_title="Galileo Cleaning Room",
        layout="wide",
    )

    st.title("Galileo Cleaning Room")
    st.caption("Pulizia e mappatura dati da galileo_archive.db")

    conn = get_connection()
    init_mapping_tables(conn)

    section = st.sidebar.radio(
        "Navigazione",
        ["Dashboard", "Mappatura Categorie", "Unificazione Autori"],
    )

    if section == "Dashboard":
        render_dashboard(conn)
    elif section == "Mappatura Categorie":
        render_category_mapping(conn)
    elif section == "Unificazione Autori":
        render_author_unification(conn)

    conn.close()


if __name__ == "__main__":
    main()
