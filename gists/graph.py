"""Graph — cross-source entity store with resolution and change tracking."""

import hashlib
import json
import re
import sqlite3
import time
import unicodedata

__repld_usage__ = "g = Graph('./network.db')"


class Graph:
    """Cross-source entity graph — resolve, observe, link, query."""

    def __init__(self, db_path: str = "./network.db") -> None:
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id         TEXT PRIMARY KEY,
                type       TEXT NOT NULL,  -- person, company
                name       TEXT NOT NULL,
                meta       TEXT DEFAULT '{}',  -- JSON blob for source-specific data
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS identifiers (
                entity_id  TEXT NOT NULL REFERENCES entities(id),
                source     TEXT NOT NULL,  -- linkedin, brreg, proff, x
                key        TEXT NOT NULL,  -- member_id, public_id, org_nr, screen_name
                value      TEXT NOT NULL,
                UNIQUE(source, key, value)
            );
            CREATE INDEX IF NOT EXISTS idx_ident_lookup ON identifiers(source, key, value);
            CREATE INDEX IF NOT EXISTS idx_ident_entity ON identifiers(entity_id);

            CREATE TABLE IF NOT EXISTS observations (
                entity_id   TEXT NOT NULL REFERENCES entities(id),
                source      TEXT NOT NULL,
                field       TEXT NOT NULL,
                value       TEXT,
                observed_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_obs_entity ON observations(entity_id, field);

            CREATE TABLE IF NOT EXISTS links (
                from_id     TEXT NOT NULL REFERENCES entities(id),
                to_id       TEXT NOT NULL REFERENCES entities(id),
                relation    TEXT NOT NULL,  -- employee, board_member, founder, investor, follows
                source      TEXT NOT NULL,
                detail      TEXT,           -- role title, board role, etc.
                observed_at REAL NOT NULL,
                UNIQUE(from_id, to_id, relation, source)
            );
            CREATE INDEX IF NOT EXISTS idx_links_from ON links(from_id);
            CREATE INDEX IF NOT EXISTS idx_links_to ON links(to_id);

            CREATE TABLE IF NOT EXISTS merge_log (
                winner_id   TEXT NOT NULL,
                loser_id    TEXT NOT NULL,
                reason      TEXT NOT NULL,
                merged_at   REAL NOT NULL
            );
        """)
        self._db.commit()

    # ------------------------------------------------------------------
    # Resolution: find or create an entity
    # ------------------------------------------------------------------

    def resolve(
        self,
        type: str,
        name: str,
        identifiers: dict[str, str] | None = None,
        hints: dict | None = None,
    ) -> str:
        """Find or create an entity. Returns entity ID.

        identifiers: {"source:key": "value", ...} e.g. {"linkedin:member_id": "ACoAA..."}
        hints: optional context for fuzzy matching, e.g. {"company": "Attensi"}
        """
        # 1. Exact identifier match
        if identifiers:
            for spec, value in identifiers.items():
                source, key = spec.split(":", 1)
                row = self._db.execute(
                    "SELECT entity_id FROM identifiers WHERE source=? AND key=? AND value=?",
                    (source, key, value),
                ).fetchone()
                if row:
                    eid = row["entity_id"]
                    # Add any new identifiers to existing entity
                    self._add_identifiers(eid, identifiers)
                    self._touch(eid, name)
                    return eid

        # 2. Name + type match (moderate: require shared company hint)
        norm = _normalize_name(name)
        if norm and hints and hints.get("company"):
            company_norm = _normalize_name(hints["company"])
            candidates = self._db.execute(
                "SELECT id, name FROM entities WHERE type=?", (type,)
            ).fetchall()
            for c in candidates:
                if _normalize_name(c["name"]) == norm:
                    # Check if they share a company link
                    if self._shares_company(c["id"], company_norm):
                        self._add_identifiers(c["id"], identifiers)
                        self._touch(c["id"], name)
                        return c["id"]

        # 3. Pure normalized name match (same type, no company required)
        if norm:
            candidates = self._db.execute(
                "SELECT id, name FROM entities WHERE type=?", (type,)
            ).fetchall()
            for c in candidates:
                if _normalize_name(c["name"]) == norm:
                    self._add_identifiers(c["id"], identifiers)
                    self._touch(c["id"], name)
                    return c["id"]

        # 4. Create new entity
        eid = _make_id(type, name)
        now = time.time()
        self._db.execute(
            "INSERT INTO entities (id, type, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (eid, type, name, now, now),
        )
        self._add_identifiers(eid, identifiers)
        self._db.commit()
        return eid

    def _add_identifiers(self, eid: str, identifiers: dict[str, str] | None):
        if not identifiers:
            return
        for spec, value in identifiers.items():
            source, key = spec.split(":", 1)
            self._db.execute(
                "INSERT OR IGNORE INTO identifiers (entity_id, source, key, value) VALUES (?, ?, ?, ?)",
                (eid, source, key, value),
            )
        self._db.commit()

    def _touch(self, eid: str, name: str | None = None):
        now = time.time()
        if name:
            self._db.execute(
                "UPDATE entities SET updated_at=?, name=? WHERE id=?", (now, name, eid)
            )
        else:
            self._db.execute("UPDATE entities SET updated_at=? WHERE id=?", (now, eid))
        self._db.commit()

    def _shares_company(self, entity_id: str, company_norm: str) -> bool:
        """Check if an entity has a link to a company matching the normalized name."""
        rows = self._db.execute(
            """SELECT e.name FROM links l
               JOIN entities e ON e.id = l.to_id
               WHERE l.from_id=? AND e.type='company'""",
            (entity_id,),
        ).fetchall()
        return any(_normalize_name(r["name"]) == company_norm for r in rows)

    # ------------------------------------------------------------------
    # Ingest: one-call resolve + meta + observe + link
    # ------------------------------------------------------------------

    def ingest(
        self,
        source: str,
        type: str,
        name: str,
        identifiers: dict[str, str] | None = None,
        data: dict | None = None,
        fields: dict[str, str] | None = None,
        link_to: str | None = None,
        relation: str | None = None,
        detail: str | None = None,
        hints: dict | None = None,
    ) -> str:
        """One-call entity ingestion: resolve + put_meta + observe + link.

        source: 'linkedin', 'brreg', 'proff', 'x'
        identifiers: {"key": "value"} — auto-prefixed with source
        data: full source-specific dict → stored as meta[source]
        fields: {"headline": "CTO at X"} → tracked observations
        link_to: entity_id to link to
        relation: link relation type (employee, board_member, etc.)
        detail: link detail (role title, etc.)
        """
        # Prefix identifiers with source if not already
        prefixed = {}
        if identifiers:
            for k, v in identifiers.items():
                prefixed[f"{source}:{k}" if ":" not in k else k] = v

        eid = self.resolve(type, name, prefixed or None, hints)

        if data:
            self.put_meta(eid, source, data)

        if fields:
            for field, value in fields.items():
                self.observe(eid, source, field, value)

        if link_to and relation:
            self.link(eid, link_to, relation, source, detail)

        return eid

    # ------------------------------------------------------------------
    # Observations: track field values over time
    # ------------------------------------------------------------------

    def observe(self, entity_id: str, source: str, field: str, value: str):
        """Record a field observation. Only writes if the value changed."""
        last = self._db.execute(
            """SELECT value FROM observations
               WHERE entity_id=? AND source=? AND field=?
               ORDER BY observed_at DESC LIMIT 1""",
            (entity_id, source, field),
        ).fetchone()
        if last and last["value"] == value:
            return  # No change
        self._db.execute(
            "INSERT INTO observations (entity_id, source, field, value, observed_at) VALUES (?, ?, ?, ?, ?)",
            (entity_id, source, field, value, time.time()),
        )
        self._db.commit()

    def history(self, entity_id: str, field: str | None = None) -> list[dict]:
        """Get observation history for an entity, optionally filtered by field."""
        if field:
            rows = self._db.execute(
                """SELECT source, field, value, observed_at FROM observations
                   WHERE entity_id=? AND field=? ORDER BY observed_at DESC""",
                (entity_id, field),
            ).fetchall()
        else:
            rows = self._db.execute(
                """SELECT source, field, value, observed_at FROM observations
                   WHERE entity_id=? ORDER BY observed_at DESC""",
                (entity_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Links: relationships between entities
    # ------------------------------------------------------------------

    def link(
        self,
        from_id: str,
        to_id: str,
        relation: str,
        source: str,
        detail: str | None = None,
    ):
        """Create or update a relationship between two entities."""
        self._db.execute(
            """INSERT INTO links (from_id, to_id, relation, source, detail, observed_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(from_id, to_id, relation, source)
               DO UPDATE SET detail=excluded.detail, observed_at=excluded.observed_at""",
            (from_id, to_id, relation, source, detail, time.time()),
        )
        self._db.commit()

    def links_from(self, entity_id: str, relation: str | None = None) -> list[dict]:
        """Get outgoing links from an entity."""
        if relation:
            rows = self._db.execute(
                """SELECT l.*, e.name as to_name, e.type as to_type
                   FROM links l JOIN entities e ON e.id = l.to_id
                   WHERE l.from_id=? AND l.relation=?
                   ORDER BY l.observed_at DESC""",
                (entity_id, relation),
            ).fetchall()
        else:
            rows = self._db.execute(
                """SELECT l.*, e.name as to_name, e.type as to_type
                   FROM links l JOIN entities e ON e.id = l.to_id
                   WHERE l.from_id=?
                   ORDER BY l.observed_at DESC""",
                (entity_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def links_to(self, entity_id: str, relation: str | None = None) -> list[dict]:
        """Get incoming links to an entity."""
        if relation:
            rows = self._db.execute(
                """SELECT l.*, e.name as from_name, e.type as from_type
                   FROM links l JOIN entities e ON e.id = l.from_id
                   WHERE l.to_id=? AND l.relation=?
                   ORDER BY l.observed_at DESC""",
                (entity_id, relation),
            ).fetchall()
        else:
            rows = self._db.execute(
                """SELECT l.*, e.name as from_name, e.type as from_type
                   FROM links l JOIN entities e ON e.id = l.from_id
                   WHERE l.to_id=?
                   ORDER BY l.observed_at DESC""",
                (entity_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def put_meta(self, entity_id: str, source: str, data: dict):
        """Store source-specific metadata as JSON (merged into entity.meta[source])."""
        row = self._db.execute(
            "SELECT meta FROM entities WHERE id=?", (entity_id,)
        ).fetchone()
        if not row:
            return
        meta = json.loads(row["meta"] or "{}")
        meta[source] = data
        self._db.execute(
            "UPDATE entities SET meta=?, updated_at=? WHERE id=?",
            (json.dumps(meta), time.time(), entity_id),
        )
        self._db.commit()

    def get(self, entity_id: str) -> dict | None:
        """Get an entity with its identifiers, metadata, and latest observations."""
        row = self._db.execute(
            "SELECT * FROM entities WHERE id=?", (entity_id,)
        ).fetchone()
        if not row:
            return None
        entity = dict(row)
        entity["meta"] = json.loads(entity.get("meta") or "{}")
        entity["identifiers"] = {
            f"{r['source']}:{r['key']}": r["value"]
            for r in self._db.execute(
                "SELECT source, key, value FROM identifiers WHERE entity_id=?",
                (entity_id,),
            ).fetchall()
        }
        # Latest value per field
        fields = self._db.execute(
            """SELECT DISTINCT field FROM observations WHERE entity_id=?""",
            (entity_id,),
        ).fetchall()
        latest = {}
        for f in fields:
            val = self._db.execute(
                """SELECT value, source, observed_at FROM observations
                   WHERE entity_id=? AND field=? ORDER BY observed_at DESC LIMIT 1""",
                (entity_id, f["field"]),
            ).fetchone()
            if val:
                latest[f["field"]] = {"value": val["value"], "source": val["source"]}
        entity["fields"] = latest
        return entity

    def find(self, source: str, key: str, value: str) -> str | None:
        """Look up entity ID by a specific identifier."""
        row = self._db.execute(
            "SELECT entity_id FROM identifiers WHERE source=? AND key=? AND value=?",
            (source, key, value),
        ).fetchone()
        return row["entity_id"] if row else None

    def search(self, name_pattern: str, type: str | None = None) -> list[dict]:
        """Search entities by name (SQL LIKE pattern)."""
        if type:
            rows = self._db.execute(
                "SELECT * FROM entities WHERE name LIKE ? AND type=? ORDER BY updated_at DESC",
                (f"%{name_pattern}%", type),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM entities WHERE name LIKE ? ORDER BY updated_at DESC",
                (f"%{name_pattern}%",),
            ).fetchall()
        return [dict(r) for r in rows]

    def merge(self, winner_id: str, loser_id: str, reason: str = "manual"):
        """Merge loser entity into winner. Moves all identifiers, observations, links."""
        # Move identifiers
        self._db.execute(
            "UPDATE OR IGNORE identifiers SET entity_id=? WHERE entity_id=?",
            (winner_id, loser_id),
        )
        # Move observations
        self._db.execute(
            "UPDATE observations SET entity_id=? WHERE entity_id=?",
            (winner_id, loser_id),
        )
        # Move links (both directions)
        self._db.execute(
            "UPDATE OR IGNORE links SET from_id=? WHERE from_id=?",
            (winner_id, loser_id),
        )
        self._db.execute(
            "UPDATE OR IGNORE links SET to_id=? WHERE to_id=?",
            (winner_id, loser_id),
        )
        # Log and delete
        self._db.execute(
            "INSERT INTO merge_log (winner_id, loser_id, reason, merged_at) VALUES (?, ?, ?, ?)",
            (winner_id, loser_id, reason, time.time()),
        )
        self._db.execute("DELETE FROM identifiers WHERE entity_id=?", (loser_id,))
        self._db.execute(
            "DELETE FROM links WHERE from_id=? OR to_id=?", (loser_id, loser_id)
        )
        self._db.execute("DELETE FROM entities WHERE id=?", (loser_id,))
        self._db.commit()

    def stats(self) -> dict:
        """Database summary."""
        return {
            "entities": self._db.execute("SELECT COUNT(*) c FROM entities").fetchone()[
                "c"
            ],
            "persons": self._db.execute(
                "SELECT COUNT(*) c FROM entities WHERE type='person'"
            ).fetchone()["c"],
            "companies": self._db.execute(
                "SELECT COUNT(*) c FROM entities WHERE type='company'"
            ).fetchone()["c"],
            "identifiers": self._db.execute(
                "SELECT COUNT(*) c FROM identifiers"
            ).fetchone()["c"],
            "observations": self._db.execute(
                "SELECT COUNT(*) c FROM observations"
            ).fetchone()["c"],
            "links": self._db.execute("SELECT COUNT(*) c FROM links").fetchone()["c"],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    """Normalize a name for comparison: lowercase, strip accents, collapse whitespace."""
    # NFD decompose, strip combining marks (accents)
    s = unicodedata.normalize("NFD", name)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower().strip()
    # Remove single-letter middle initials with dots: "Ole J. Rosendahl" → "Ole Rosendahl"
    s = re.sub(r"\b[a-z]\.\s*", "", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s)
    return s


def _make_id(type: str, name: str) -> str:
    """Generate a short stable ID from type + name + timestamp."""
    raw = f"{type}:{name}:{time.time()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]
