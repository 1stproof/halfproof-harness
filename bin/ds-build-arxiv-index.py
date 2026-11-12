#!/usr/bin/env python3
"""Build the local arXiv metadata FTS5 index from Kaggle JSON lines."""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time


def text(value):
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, (list, tuple)):
                parts.append(" ".join(str(piece) for piece in item if piece is not None))
            elif isinstance(item, dict):
                parts.append(str(item.get("name") or item.get("author") or ""))
        return " ".join(" ".join(parts).split())
    return ""


def versions(value):
    if not isinstance(value, list):
        return []
    answer = []
    for index, item in enumerate(value, 1):
        label = item.get("version") if isinstance(item, dict) else str(item)
        label = str(label or f"v{index}")
        if label.startswith("v") and label[1:].isdigit():
            answer.append(label)
    return answer


def build(source, output, limit=None):
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(fd)
    count = 0
    started = time.time()
    try:
        connection = sqlite3.connect(temporary)
        connection.executescript("""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=MEMORY;
            CREATE TABLE papers (
                rowid INTEGER PRIMARY KEY,
                id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                authors TEXT NOT NULL,
                abstract TEXT NOT NULL,
                categories TEXT NOT NULL,
                versions TEXT NOT NULL,
                update_date TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE papers_fts USING fts5(
                id, title, authors, abstract, categories,
                content='papers', content_rowid='rowid',
                tokenize='unicode61 remove_diacritics 2'
            );
        """)
        batch = []
        with source.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if limit is not None and count >= limit:
                    break
                try:
                    record = json.loads(line)
                    arxiv_id = str(record["id"]).strip()
                except (ValueError, KeyError, TypeError) as exc:
                    raise RuntimeError(f"invalid JSON record on line {line_number}: {exc}") from exc
                batch.append((
                    arxiv_id,
                    text(record.get("title")),
                    text(record.get("authors") or record.get("authors_parsed")),
                    text(record.get("abstract")),
                    text(record.get("categories")),
                    json.dumps(versions(record.get("versions")), separators=(",", ":")),
                    str(record.get("update_date") or ""),
                ))
                count += 1
                if len(batch) >= 10000:
                    connection.executemany(
                        "INSERT OR REPLACE INTO papers(id,title,authors,abstract,categories,versions,update_date) VALUES(?,?,?,?,?,?,?)",
                        batch,
                    )
                    connection.commit()
                    batch.clear()
                    if count % 100000 == 0:
                        print(f"indexed {count} records in {time.time() - started:.1f}s", flush=True)
        if batch:
            connection.executemany(
                "INSERT OR REPLACE INTO papers(id,title,authors,abstract,categories,versions,update_date) VALUES(?,?,?,?,?,?,?)",
                batch,
            )
            connection.commit()
        connection.execute("INSERT INTO papers_fts(papers_fts) VALUES('rebuild')")
        connection.execute("INSERT INTO papers_fts(papers_fts) VALUES('optimize')")
        connection.execute("CREATE INDEX papers_id_nocase ON papers(id COLLATE NOCASE)")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {result}")
        connection.close()
        os.chmod(temporary, 0o444)
        os.replace(temporary, output)
        print(f"complete records={count} bytes={output.stat().st_size} elapsed={time.time() - started:.1f}s", flush=True)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    build(args.source.resolve(), args.output.resolve(), args.limit)


if __name__ == "__main__":
    main()
