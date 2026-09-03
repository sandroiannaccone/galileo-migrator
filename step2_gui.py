#!/usr/bin/env python3
"""Cleaning Room GUI per mappare categorie e unificare autori su galileo_archive.db."""

import sqlite3
import time

import pandas as pd
import streamlit as st

DB_FILE = "galileo_archive.db"
BASE_ARTICLE_URL = "https://www.galileonet.it/"

TARGET_CATEGORIES = [
    "Corpo e Mente",
    "Pianeta e Animali",
    "Spazio ed Esplorazione",
    "Futuri",
    "Materia e Numeri",
    "Noi Umani",
    "🗑️ CESTINA",
]

TRASH_OPTION = "🗑️ CESTINA"
CATEGORY_SELECT_OPTIONS = [""] + TARGET_CATEGORIES
RICERCA_ITALIA_CAT = "Ricerca d'Italia"
ORFANI_RICERCA_ITALIA = "Orfani Ricerca d'Italia"
EXTRA_CAT = "Extra"
FALLBACK_AUTHOR = "Redazione di Galileo"

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
    wp_id             INTEGER PRIMARY KEY,
    target_category   TEXT,
    is_deleted        INTEGER DEFAULT 0
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
    try:
        conn.execute(
            "ALTER TABLE articles ADD COLUMN is_ricerca_italia INTEGER DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "ALTER TABLE article_overrides ADD COLUMN is_deleted INTEGER DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "ALTER TABLE article_overrides RENAME COLUMN new_cat TO target_category"
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


def build_article_url(slug: str | None) -> str:
    if not slug or pd.isna(slug) or not str(slug).strip():
        return ""
    return f"{BASE_ARTICLE_URL}{str(slug).strip().lstrip('/')}/"


def build_article_link_display_url(slug: str | None, title: str | None) -> str:
    """URL per LinkColumn: il titolo è nel fragment # (ignorato dal browser)."""
    base_url = build_article_url(slug)
    if not base_url:
        return ""
    title_text = "" if not title or pd.isna(title) else str(title).strip()
    return f"{base_url}#{title_text}"


ARTICLE_LINK_DISPLAY_TEXT_REGEX = r"https://.*?#(.*)$"


def add_article_url_column(
    df: pd.DataFrame,
    slug_col: str = "slug",
    title_col: str = "title",
    url_col: str = "url",
) -> pd.DataFrame:
    result = df.copy()
    if slug_col not in result.columns:
        result[url_col] = ""
        return result

    titles = (
        result[title_col].fillna("").astype(str)
        if title_col in result.columns
        else pd.Series([""] * len(result), index=result.index)
    )
    result[url_col] = [
        build_article_link_display_url(slug, title)
        for slug, title in zip(result[slug_col], titles)
    ]
    return result


def get_article_link_column_config() -> dict:
    return {
        "url": st.column_config.LinkColumn(
            "Apri Articolo",
            display_text=ARTICLE_LINK_DISPLAY_TEXT_REGEX,
            help="Apri l'articolo originale su galileonet.it",
        ),
    }


def article_contains_category(categories_str: str, category: str) -> bool:
    if not categories_str or pd.isna(categories_str):
        return False
    return category in [c.strip() for c in str(categories_str).split(",")]


def remove_category_from_list(categories_str: str, category: str) -> str:
    if not categories_str or pd.isna(categories_str):
        return ""
    parts = [c.strip() for c in str(categories_str).split(",")]
    return ", ".join(c for c in parts if c and c != category)


def categories_effectively_empty(categories_str: str) -> bool:
    if not categories_str or not str(categories_str).strip():
        return True
    return all(not part.strip() for part in str(categories_str).split(","))


def run_ricerca_italia_prep(conn: sqlite3.Connection) -> int:
    """Flagga Ricerca d'Italia, rimuove la categoria e assegna gli orfani."""
    rows = conn.execute(
        "SELECT wp_id, categories FROM articles WHERE is_deleted = 0"
    ).fetchall()

    affected = [
        (wp_id, categories_str)
        for wp_id, categories_str in rows
        if article_contains_category(categories_str, RICERCA_ITALIA_CAT)
    ]
    if not affected:
        return 0

    conn.executemany(
        "UPDATE articles SET is_ricerca_italia = 1 WHERE wp_id = ?",
        [(wp_id,) for wp_id, _ in affected],
    )

    updated = 0
    for wp_id, categories_str in affected:
        cleaned = remove_category_from_list(categories_str, RICERCA_ITALIA_CAT)
        if categories_effectively_empty(cleaned):
            cleaned = ORFANI_RICERCA_ITALIA
        conn.execute(
            "UPDATE articles SET categories = ? WHERE wp_id = ?",
            (cleaned, wp_id),
        )
        updated += 1

    conn.commit()
    return updated


def run_nuke_extra(conn: sqlite3.Connection) -> int:
    """Cestina tutti gli articoli attivi che contengono la categoria Extra."""
    rows = conn.execute(
        "SELECT wp_id, categories FROM articles WHERE is_deleted = 0"
    ).fetchall()

    target_wp_ids = [
        wp_id
        for wp_id, categories_str in rows
        if article_contains_category(categories_str, EXTRA_CAT)
    ]
    if not target_wp_ids:
        return 0

    count = insert_article_overrides(
        conn, target_wp_ids, TRASH_OPTION, is_deleted=1
    )
    conn.commit()
    return count


def get_unmapped_wp_ids_for_category(
    conn: sqlite3.Connection, old_cat: str
) -> list[int]:
    """Trova wp_id non ancora in article_overrides con match sulla categoria."""
    rows = conn.execute(
        """
        SELECT a.wp_id, a.categories
        FROM articles a
        LEFT JOIN article_overrides ao ON a.wp_id = ao.wp_id
        WHERE a.is_deleted = 0
          AND ao.wp_id IS NULL
        """
    ).fetchall()
    return [
        wp_id
        for wp_id, categories_str in rows
        if article_contains_category(categories_str, old_cat)
    ]


def insert_article_overrides(
    conn: sqlite3.Connection,
    wp_ids: list[int],
    target_category: str,
    is_deleted: int,
) -> int:
    for wp_id in wp_ids:
        conn.execute(
            """
            INSERT OR REPLACE INTO article_overrides
                (wp_id, target_category, is_deleted)
            VALUES (?, ?, ?)
            """,
            (wp_id, target_category, is_deleted),
        )
        if is_deleted:
            conn.execute(
                "UPDATE articles SET is_deleted = 1 WHERE wp_id = ?", (wp_id,)
            )
    return len(wp_ids)


def build_category_counts_df(conn: sqlite3.Connection) -> pd.DataFrame:
    """Esplode le categorie comma-separated e conta gli articoli per categoria."""
    articles_df = pd.read_sql_query(
        """
        SELECT categories FROM articles
        WHERE is_deleted = 0
          AND categories IS NOT NULL
          AND categories != ''
          AND wp_id NOT IN (SELECT wp_id FROM article_overrides)
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


def get_irriducibili_df(conn: sqlite3.Connection) -> pd.DataFrame:
    """Articoli attivi non mappati e assenti dalla tabella globale di raggruppamento."""
    df = pd.read_sql_query(
        """
        SELECT wp_id, title, categories, slug
        FROM articles
        WHERE is_deleted = 0
          AND wp_id NOT IN (SELECT wp_id FROM article_overrides)
        ORDER BY title
        """,
        conn,
    )
    if df.empty:
        return pd.DataFrame(
            columns=["wp_id", "Titolo", "url", "Categorie", "Assegna Categoria"]
        )

    def has_groupable_category(categories_str: str) -> bool:
        if not categories_str or pd.isna(categories_str) or not str(categories_str).strip():
            return False
        return any(part.strip() for part in str(categories_str).split(","))

    df = df[~df["categories"].apply(has_groupable_category)].copy()
    if df.empty:
        return pd.DataFrame(
            columns=["wp_id", "Titolo", "url", "Categorie", "Assegna Categoria"]
        )

    df = df.rename(columns={"title": "Titolo", "categories": "Categorie"})
    df = add_article_url_column(df, title_col="Titolo")
    df["Assegna Categoria"] = ""
    return df[["wp_id", "Titolo", "url", "Categorie", "Assegna Categoria"]]


def save_irriducibili(conn: sqlite3.Connection, df: pd.DataFrame) -> tuple[int, int]:
    """Salva override per articoli irriducibili."""
    saved = 0
    deleted = 0
    for _, row in df.iterrows():
        assignment = row.get("Assegna Categoria", "")
        if pd.isna(assignment) or not str(assignment).strip():
            continue

        wp_id = int(row["wp_id"])
        assignment = str(assignment).strip()

        if assignment == TRASH_OPTION:
            insert_article_overrides(conn, [wp_id], TRASH_OPTION, is_deleted=1)
            deleted += 1
        elif assignment in TARGET_CATEGORIES:
            insert_article_overrides(conn, [wp_id], assignment, is_deleted=0)
            saved += 1

    conn.commit()
    return saved, deleted


def get_articles_for_category(
    conn: sqlite3.Connection, category: str, hide_mapped: bool = True
) -> pd.DataFrame:
    """Filtra gli articoli attivi che contengono la categoria selezionata."""
    if hide_mapped:
        articles_df = pd.read_sql_query(
            """
            SELECT a.wp_id, a.title, a.pub_date, a.categories, a.slug
            FROM articles a
            LEFT JOIN article_overrides ao ON a.wp_id = ao.wp_id
            WHERE a.is_deleted = 0
              AND ao.wp_id IS NULL
            """,
            conn,
        )
    else:
        articles_df = pd.read_sql_query(
            """
            SELECT wp_id, title, pub_date, categories, slug
            FROM articles
            WHERE is_deleted = 0
            """,
            conn,
        )

    mask = articles_df["categories"].apply(
        lambda x: article_contains_category(x, category)
    )
    filtered = articles_df[mask].copy()

    if not hide_mapped:
        overrides_df = pd.read_sql_query(
            "SELECT wp_id, target_category FROM article_overrides", conn
        )
        if not overrides_df.empty:
            filtered = filtered.merge(overrides_df, on="wp_id", how="left")
            filtered["Assegnazione Singola"] = filtered["target_category"].fillna("")
            filtered = filtered.drop(columns=["target_category"])
        else:
            filtered["Assegnazione Singola"] = ""
    else:
        filtered["Assegnazione Singola"] = ""

    filtered = filtered.rename(
        columns={
            "title": "Titolo",
            "pub_date": "Data",
            "categories": "Categorie Originali",
        }
    )
    filtered = add_article_url_column(filtered, title_col="Titolo")
    return filtered[
        [
            "wp_id",
            "Titolo",
            "url",
            "Data",
            "Categorie Originali",
            "Assegnazione Singola",
        ]
    ]


def get_mapping_stats(conn: sqlite3.Connection) -> tuple[int, int, int]:
    total = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE is_deleted = 0"
    ).fetchone()[0]
    mapped, to_map = get_prominent_mapping_counts(conn)
    return total, mapped, to_map


def get_prominent_mapping_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    """Contatori principali: categorizzati vs restanti da mappare manualmente."""
    conteggio_mappati = conn.execute(
        "SELECT COUNT(*) FROM article_overrides WHERE is_deleted = 0"
    ).fetchone()[0]
    conteggio_da_mappare = conn.execute(
        """
        SELECT COUNT(*)
        FROM articles
        WHERE is_deleted = 0
          AND wp_id NOT IN (SELECT wp_id FROM article_overrides)
        """
    ).fetchone()[0]
    return conteggio_mappati, conteggio_da_mappare


def render_prominent_mapping_metrics(conn: sqlite3.Connection) -> None:
    """Metriche principali in evidenza sotto il titolo della pagina."""
    conteggio_mappati, conteggio_da_mappare = get_prominent_mapping_counts(conn)

    prev_mappati = st.session_state.get("prev_conteggio_mappati")
    prev_da_mappare = st.session_state.get("prev_conteggio_da_mappare")

    col1, col2 = st.columns(2)
    with col1:
        st.metric(
            label="✅ Articoli Categorizzati",
            value=conteggio_mappati,
            delta=None if prev_mappati is None else conteggio_mappati - prev_mappati,
        )
    with col2:
        st.metric(
            label="⏳ Da Mappare (Manuali)",
            value=conteggio_da_mappare,
            delta=None
            if prev_da_mappare is None
            else conteggio_da_mappare - prev_da_mappare,
            delta_color="inverse",
        )

    st.session_state["prev_conteggio_mappati"] = conteggio_mappati
    st.session_state["prev_conteggio_da_mappare"] = conteggio_da_mappare


def get_last_mapped_article(conn: sqlite3.Connection) -> tuple[str, str] | None:
    """Restituisce titolo e categoria dell'ultimo override inserito."""
    row = conn.execute(
        """
        SELECT a.title, o.target_category
        FROM article_overrides o
        JOIN articles a ON o.wp_id = a.wp_id
        ORDER BY o.ROWID DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return row[0] or "Senza titolo", row[1] or "—"


def render_last_mapped_feed(conn: sqlite3.Connection) -> None:
    """Feed live con l'ultimo articolo categorizzato."""
    last_mapped = get_last_mapped_article(conn)
    if not last_mapped:
        return

    titolo, categoria = last_mapped
    st.success(
        f"⚡ **Ultimo articolo mappato:** *{titolo}* ➡️ **{categoria}**"
    )


def get_mapping_category_stats_df(conn: sqlite3.Connection) -> pd.DataFrame:
    """Distribuzione degli articoli per categoria target (solo override attivi)."""
    return pd.read_sql_query(
        """
        SELECT target_category, COUNT(*) AS conteggio
        FROM article_overrides
        WHERE is_deleted = 0
        GROUP BY target_category
        ORDER BY conteggio DESC
        """,
        conn,
    )


def render_mapping_statistics(conn: sqlite3.Connection) -> None:
    """Mostra la distribuzione delle categorie assegnate."""
    stats_df = get_mapping_category_stats_df(conn)
    if stats_df.empty:
        st.caption("Nessun articolo categorizzato ancora.")
        return

    chart_df = stats_df.set_index("target_category")
    st.bar_chart(chart_df, height=280)

    with st.expander("Dettaglio numerico"):
        st.dataframe(
            stats_df.rename(
                columns={
                    "target_category": "Categoria",
                    "conteggio": "Conteggio",
                }
            ),
            hide_index=True,
            width="stretch",
        )


def get_mapped_articles_df(conn: sqlite3.Connection) -> pd.DataFrame:
    mapped_df = pd.read_sql_query(
        """
        SELECT
            a.wp_id,
            a.title AS titolo,
            a.slug,
            a.categories AS categorie_originali,
            ao.target_category AS target_category
        FROM articles a
        JOIN article_overrides ao ON a.wp_id = ao.wp_id
        WHERE a.is_deleted = 0
        ORDER BY ao.target_category, a.title
        """,
        conn,
    )
    return add_article_url_column(mapped_df, title_col="titolo")


def delete_article_override(conn: sqlite3.Connection, wp_id: int) -> bool:
    cursor = conn.execute(
        "DELETE FROM article_overrides WHERE wp_id = ?", (wp_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


def search_articles(conn: sqlite3.Connection, query: str) -> list[dict]:
    query = query.strip()
    if not query:
        return []

    if query.isdigit():
        rows = conn.execute(
            """
            SELECT wp_id, title, slug, excerpt, categories, is_ricerca_italia
            FROM articles
            WHERE is_deleted = 0 AND wp_id = ?
            """,
            (int(query),),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT wp_id, title, slug, excerpt, categories, is_ricerca_italia
            FROM articles
            WHERE is_deleted = 0 AND (title LIKE ? OR author LIKE ?)
            ORDER BY title
            LIMIT 20
            """,
            (f"%{query}%", f"%{query}%"),
        ).fetchall()

    return [
        {
            "wp_id": row[0],
            "title": row[1],
            "slug": row[2],
            "url": build_article_link_display_url(row[2], row[1]),
            "excerpt": row[3],
            "categories": row[4],
            "is_ricerca_italia": row[5],
        }
        for row in rows
    ]


def update_article(
    conn: sqlite3.Connection,
    wp_id: int,
    title: str,
    excerpt: str,
    categories: str,
    is_ricerca_italia: bool,
) -> None:
    conn.execute(
        """
        UPDATE articles
        SET title = ?, excerpt = ?, categories = ?, is_ricerca_italia = ?
        WHERE wp_id = ?
        """,
        (title, excerpt, categories, int(is_ricerca_italia), wp_id),
    )
    conn.commit()


def extract_unique_authors(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        """
        SELECT author, COUNT(*) AS conteggio
        FROM articles
        WHERE is_deleted = 0
          AND author IS NOT NULL
          AND author != ''
        GROUP BY author
        ORDER BY conteggio DESC
        """
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def save_category_map(
    conn: sqlite3.Connection, df: pd.DataFrame
) -> tuple[int, int, int]:
    """Salva regole globali espandendole in override granulari."""
    mapped = 0
    overrides_created = 0
    deleted = 0
    for _, row in df.iterrows():
        new_cat = row.get("Nuova Categoria", "")
        if pd.isna(new_cat) or not str(new_cat).strip():
            continue

        old_cat = row["Vecchia Categoria"]
        new_cat = str(new_cat).strip()
        wp_ids = get_unmapped_wp_ids_for_category(conn, old_cat)
        if not wp_ids:
            continue

        if new_cat == TRASH_OPTION:
            overrides_created += insert_article_overrides(
                conn, wp_ids, TRASH_OPTION, is_deleted=1
            )
            deleted += len(wp_ids)
            conn.execute(
                "INSERT OR REPLACE INTO category_map (old_cat, new_cat) VALUES (?, ?)",
                (old_cat, new_cat),
            )
            mapped += 1
        elif new_cat in TARGET_CATEGORIES:
            overrides_created += insert_article_overrides(
                conn, wp_ids, new_cat, is_deleted=0
            )
            conn.execute(
                "INSERT OR REPLACE INTO category_map (old_cat, new_cat) VALUES (?, ?)",
                (old_cat, new_cat),
            )
            mapped += 1

    conn.commit()
    return mapped, overrides_created, deleted


def save_author_map(
    conn: sqlite3.Connection, df: pd.DataFrame, apply_fallback: bool = False
) -> int:
    saved = 0
    for _, row in df.iterrows():
        new_author = row.get("Nuovo Autore", "")
        if pd.isna(new_author) or not str(new_author).strip():
            if apply_fallback:
                new_author = FALLBACK_AUTHOR
            else:
                continue
        else:
            new_author = str(new_author).strip()

        conn.execute(
            "INSERT OR REPLACE INTO author_map (old_author, new_author) VALUES (?, ?)",
            (row["Vecchio Autore"], new_author),
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
                """
                INSERT OR REPLACE INTO article_overrides
                    (wp_id, target_category, is_deleted)
                VALUES (?, ?, 1)
                """,
                (wp_id, TRASH_OPTION),
            )
            conn.execute(
                "UPDATE articles SET is_deleted = 1 WHERE wp_id = ?", (wp_id,)
            )
            deleted += 1
        elif assignment in TARGET_CATEGORIES:
            conn.execute(
                """
                INSERT OR REPLACE INTO article_overrides
                    (wp_id, target_category, is_deleted)
                VALUES (?, ?, 0)
                """,
                (wp_id, assignment),
            )
            saved += 1

    conn.commit()
    return saved, deleted


def render_system_actions(conn: sqlite3.Connection) -> None:
    with st.expander("🛠️ Azioni Automatiche di Sistema", expanded=True):
        st.caption(
            "Operazioni ETL una tantum per normalizzare i dati prima della mappatura manuale."
        )
        if st.button("⚡ Esegui Pre-pulizia Ricerca d'Italia"):
            count = run_ricerca_italia_prep(conn)
            st.success(f"Pre-pulizia completata: {count} record aggiornati.")
            time.sleep(2)
            st.rerun()

        if st.button("🗑️ Cestina tutti gli articoli contenenti 'Extra'"):
            count = run_nuke_extra(conn)
            st.success(
                f"Cestinamento completato: {count} articoli contenenti "
                f"«{EXTRA_CAT}» sono stati cestinati con successo."
            )
            time.sleep(2)
            st.rerun()


def render_category_mapping(conn: sqlite3.Connection) -> None:
    st.header("Mappatura Categorie")
    st.caption(
        "Associa ogni vecchia categoria WordPress a una delle nuove categorie target, "
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
        width="stretch",
        key="category_editor",
    )

    if st.button("Salva Mappatura Categorie", type="primary"):
        mapped, overrides_created, deleted = save_category_map(conn, edited_df)
        parts = []
        if mapped:
            parts.append(f"{mapped} regole in category_map")
        if overrides_created:
            parts.append(f"{overrides_created} override in article_overrides")
        if deleted:
            parts.append(f"{deleted} articoli cestinati")
        st.session_state["category_save_msg"] = (
            "Salvataggio completato: " + ", ".join(parts) + "."
            if parts
            else "Nessuna modifica da salvare."
        )
        st.rerun()

    st.divider()
    st.subheader("🕵️‍♂️ Caccia agli Irriducibili (Articoli non mappati)")

    if msg := st.session_state.pop("irriducibili_save_msg", None):
        st.success(msg)

    irriducibili_df = get_irriducibili_df(conn)
    if irriducibili_df.empty:
        st.info("Nessun articolo irriducibile: tutti gli articoli attivi sono mappati.")
    else:
        st.caption(
            f"{len(irriducibili_df)} articoli senza override e senza categorie "
            "raggruppabili nella tabella globale."
        )
        edited_irriducibili = st.data_editor(
            irriducibili_df,
            column_config={
                "wp_id": st.column_config.NumberColumn(
                    "wp_id", disabled=True, format="%d"
                ),
                "Titolo": None,
                **get_article_link_column_config(),
                "Categorie": st.column_config.TextColumn("Categorie", disabled=True),
                "Assegna Categoria": st.column_config.SelectboxColumn(
                    "Assegna Categoria",
                    options=CATEGORY_SELECT_OPTIONS,
                    required=False,
                ),
            },
            column_order=("wp_id", "url", "Categorie", "Assegna Categoria"),
            hide_index=True,
            width="stretch",
            key="irriducibili_editor",
        )

        if st.button("💾 Salva Irriducibili", type="primary"):
            saved, deleted = save_irriducibili(conn, edited_irriducibili)
            parts = []
            if saved:
                parts.append(f"{saved} articoli mappati")
            if deleted:
                parts.append(f"{deleted} articoli cestinati")
            st.session_state["irriducibili_save_msg"] = (
                "Salvataggio completato: " + ", ".join(parts) + "."
                if parts
                else "Nessuna modifica da salvare."
            )
            time.sleep(1)
            st.rerun()

    st.divider()
    st.subheader("Analisi Granulare Categorie Miste")

    old_categories = df["Vecchia Categoria"].tolist()
    if not old_categories:
        st.info("Nessuna categoria trovata negli articoli.")
    else:
        selected_cat = st.selectbox(
            "Seleziona una vecchia categoria da ispezionare",
            options=old_categories,
        )

        if selected_cat:
            if msg := st.session_state.pop("override_save_msg", None):
                st.success(msg)

            hide_mapped = st.checkbox("Nascondi articoli già mappati", value=True)
            articles_df = get_articles_for_category(
                conn, selected_cat, hide_mapped=hide_mapped
            )
            st.caption(
                f"{len(articles_df)} articoli contengono la categoria «{selected_cat}»"
                + (" (inbox: solo da mappare)" if hide_mapped else "")
            )

            edited_articles = st.data_editor(
                articles_df,
                column_config={
                    "wp_id": st.column_config.NumberColumn(
                        "wp_id", disabled=True, format="%d"
                    ),
                    "Titolo": None,
                    **get_article_link_column_config(),
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
                column_order=(
                    "wp_id",
                    "url",
                    "Data",
                    "Categorie Originali",
                    "Assegnazione Singola",
                ),
                hide_index=True,
                width="stretch",
                key=f"article_override_{selected_cat}_{hide_mapped}",
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

    st.divider()
    with st.expander("Unificazione Autori"):
        render_author_unification(conn)


def render_dashboard_undo(conn: sqlite3.Connection) -> None:
    st.header("Dashboard & Undo")

    total, mapped, to_map = get_mapping_stats(conn)
    col1, col2, col3 = st.columns(3)
    col1.metric("Totale Articoli", total)
    col2.metric("Articoli Mappati", mapped)
    col3.metric("Articoli da Mappare", to_map)

    st.subheader("Articoli con override attivo")
    mapped_df = get_mapped_articles_df(conn)
    if mapped_df.empty:
        st.info("Nessun articolo mappato tramite override singolo.")
    else:
        st.dataframe(
            mapped_df[
                [
                    "wp_id",
                    "url",
                    "titolo",
                    "categorie_originali",
                    "target_category",
                ]
            ],
            column_config={
                "wp_id": st.column_config.NumberColumn("wp_id", format="%d"),
                **get_article_link_column_config(),
                "titolo": None,
                "categorie_originali": st.column_config.TextColumn(
                    "Categorie Originali"
                ),
                "target_category": st.column_config.TextColumn("Target Category"),
            },
            column_order=(
                "wp_id",
                "url",
                "categorie_originali",
                "target_category",
            ),
            width="stretch",
            hide_index=True,
        )

    st.divider()
    st.subheader("Ripristina articolo (Undo)")
    st.caption(
        "Incolla l'ID WordPress (wp_id) per rimuovere l'override e "
        "ripristinare l'articolo nella coda di mappatura."
    )

    if msg := st.session_state.pop("undo_msg", None):
        st.success(msg)
    if err := st.session_state.pop("undo_err", None):
        st.error(err)

    with st.form("undo_override_form"):
        wp_id_input = st.text_input("wp_id articolo", placeholder="es. 12345")
        submitted = st.form_submit_button("Elimina override", type="primary")

    if submitted:
        if not wp_id_input.strip().isdigit():
            st.session_state["undo_err"] = "Inserisci un wp_id numerico valido."
        else:
            wp_id = int(wp_id_input.strip())
            if delete_article_override(conn, wp_id):
                st.session_state["undo_msg"] = (
                    f"Override rimosso per l'articolo {wp_id}. "
                    "L'articolo è di nuovo disponibile in mappatura."
                )
            else:
                st.session_state["undo_err"] = (
                    f"Nessun override trovato per l'articolo {wp_id}."
                )
        st.rerun()


def render_god_mode(conn: sqlite3.Connection) -> None:
    st.header("God Mode (CRUD)")
    st.caption(
        "Cerca e modifica direttamente un singolo articolo nella tabella base."
    )

    if msg := st.session_state.pop("god_mode_msg", None):
        st.success(msg)

    search_query = st.text_input(
        "Cerca per wp_id o parola chiave nel titolo",
        placeholder="es. 12345 oppure 'Marte'",
    )

    if not search_query.strip():
        return

    results = search_articles(conn, search_query)
    if not results:
        st.warning("Nessun articolo trovato.")
        return

    results_df = pd.DataFrame(results).rename(columns={"title": "titolo"})
    st.dataframe(
        results_df[["wp_id", "url", "titolo", "categories"]],
        column_config={
            "wp_id": st.column_config.NumberColumn("wp_id", format="%d"),
            **get_article_link_column_config(),
            "titolo": None,
            "categories": st.column_config.TextColumn("Categorie"),
        },
        column_order=("wp_id", "url", "categories"),
        width="stretch",
        hide_index=True,
    )

    if len(results) == 1:
        article = results[0]
    else:
        options = {
            f"{row['wp_id']} — {row['title']}": row for row in results
        }
        selected_label = st.selectbox(
            "Più risultati: seleziona l'articolo da modificare",
            options=list(options.keys()),
        )
        article = options[selected_label]

    wp_id = article["wp_id"]
    st.markdown(f"**Articolo selezionato:** `{wp_id}`")
    if article.get("slug"):
        st.link_button(
            "Apri articolo originale",
            build_article_url(article["slug"]),
        )

    with st.form(f"god_mode_edit_{wp_id}"):
        title = st.text_input("Titolo", value=article["title"] or "")
        abstract = st.text_area("Abstract", value=article["excerpt"] or "")
        old_categories = st.text_input(
            "old_categories", value=article["categories"] or ""
        )
        is_ricerca_italia = st.checkbox(
            "is_ricerca_italia",
            value=bool(article["is_ricerca_italia"]),
        )
        submitted = st.form_submit_button("Salva modifiche", type="primary")

    if submitted:
        update_article(
            conn,
            wp_id=wp_id,
            title=title.strip(),
            excerpt=abstract.strip(),
            categories=old_categories.strip(),
            is_ricerca_italia=is_ricerca_italia,
        )
        st.session_state["god_mode_msg"] = f"Articolo {wp_id} aggiornato con successo."
        st.rerun()


def render_author_unification(conn: sqlite3.Connection) -> None:
    st.caption(
        "Unifica varianti dello stesso autore indicando il nome corretto."
    )

    existing_map = load_author_map(conn)
    authors_with_counts = extract_unique_authors(conn)

    rows = [
        {
            "Vecchio Autore": author,
            "Conteggio Articoli": count,
            "Nuovo Autore": existing_map.get(author, ""),
        }
        for author, count in authors_with_counts
    ]
    df = pd.DataFrame(rows)

    apply_fallback = st.checkbox(
        "🔮 Assegna automaticamente 'Redazione di Galileo' a tutti gli autori lasciati vuoti"
    )

    edited_df = st.data_editor(
        df,
        column_config={
            "Vecchio Autore": st.column_config.TextColumn(
                "Vecchio Autore", disabled=True
            ),
            "Conteggio Articoli": st.column_config.NumberColumn(
                "Conteggio Articoli", disabled=True, format="%d"
            ),
            "Nuovo Autore": st.column_config.TextColumn("Nuovo Autore"),
        },
        hide_index=True,
        width="stretch",
        key="author_editor",
    )

    if st.button("Salva Unificazione Autori", type="primary"):
        saved = save_author_map(conn, edited_df, apply_fallback=apply_fallback)
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

    render_prominent_mapping_metrics(conn)
    render_last_mapped_feed(conn)
    st.divider()

    with st.sidebar:
        live_update = st.toggle("🔴 Abilita Aggiornamento Live", value=False)
        st.divider()

        total, mapped, to_map = get_mapping_stats(conn)
        col1, col2 = st.columns(2)
        col1.metric("Mappati", mapped)
        col2.metric("Da mappare", to_map)
        st.metric("Totale articoli", total)

        st.subheader("Statistiche di Mappatura")
        render_mapping_statistics(conn)

    render_system_actions(conn)
    st.divider()

    tab_mappatura, tab_dashboard, tab_god_mode = st.tabs(
        ["🗂️ Mappatura Categorie", "📊 Dashboard & Undo", "⚙️ God Mode (CRUD)"]
    )

    with tab_mappatura:
        render_category_mapping(conn)
    with tab_dashboard:
        render_dashboard_undo(conn)
    with tab_god_mode:
        render_god_mode(conn)

    conn.close()

    if live_update:
        time.sleep(3)
        st.rerun()


if __name__ == "__main__":
    main()
