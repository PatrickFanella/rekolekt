#!/usr/bin/env python3
"""Compare native search plans on a deterministic, session-local corpus.

DATABASE_URL selects a PostgreSQL test database. All fixture relations are TEMP
and disappear on disconnect; no archive tables, settings or statistics change.
"""

import json
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.search.segment_repository import SearchRepository  # noqa: E402


class Capture:
    def execute(self, sql, params):
        self.sql, self.params = sql, params
        return self

    def mappings(self):
        return self

    def all(self):
        return []


def main():
    capture = Capture()
    SearchRepository().search_native(capture, q="qualneedle", limit=10)
    legacy = """
        SELECT s.id,s.video_id,s.start_ms,s.end_ms,
          CASE WHEN s.text_tsv @@ websearch_to_tsquery('english',:q)
            THEN ts_headline('english',s.text,websearch_to_tsquery('english',:q),:headline_options)
            ELSE coalesce(v.title,s.text) END AS snippet,
          ts_rank_cd(s.text_tsv,websearch_to_tsquery('english',:q)) AS rank,
          CASE WHEN v.title ILIKE :title_q THEN 1 ELSE 0 END AS title_match
        FROM segments s JOIN videos v ON s.video_id=v.id
        WHERE (s.text_tsv @@ websearch_to_tsquery('english',:q) OR v.title ILIKE :title_q)
        ORDER BY rank DESC,title_match DESC,s.start_ms ASC LIMIT :limit OFFSET :offset
    """
    engine = create_engine(os.environ["DATABASE_URL"], hide_parameters=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SET LOCAL statement_timeout='30s'"))
            conn.execute(text("SET LOCAL track_io_timing=on"))
            for sql in [
                "CREATE TEMP TABLE videos (id bigint,title text,uploaded_at timestamptz,duration_seconds int) ON COMMIT DROP",
                "CREATE TEMP TABLE segments (id bigint,video_id bigint,start_ms int,end_ms int,text text,text_tsv tsvector) ON COMMIT DROP",
                "INSERT INTO videos SELECT i,'ordinary video '||i,NULL,100 FROM generate_series(1,1000) i",
                "INSERT INTO segments SELECT i,1+(i%1000),i,i+1,"
                "CASE WHEN i%10000=0 THEN 'qualneedle' ELSE 'ordinary transcript words' END,"
                "to_tsvector('english',CASE WHEN i%10000=0 THEN 'qualneedle' ELSE 'ordinary transcript words' END) "
                "FROM generate_series(1,100000) i",
                "CREATE INDEX ON segments USING gin(text_tsv)",
                "CREATE INDEX ON segments(video_id,start_ms)",
                "ANALYZE videos",
                "ANALYZE segments",
            ]:
                conn.execute(text(sql))
            expected = [r[0] for r in conn.execute(text(legacy), capture.params)]
            actual = [r[0] for r in conn.execute(capture.sql, capture.params)]
            assert actual == expected, "Native search results changed"
            results = []
            # Alternate the plans so both see the same warm fixture and host.
            for repetition in range(3):
                for name, sql in [("original_or", legacy), ("indexed_branches", str(capture.sql))]:
                    plan = conn.execute(
                        text("EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) " + sql), capture.params
                    ).scalar_one()[0]
                    root = plan["Plan"]
                    results.append(
                        {
                            "query": name,
                            "repetition": repetition,
                            "execution_ms": plan["Execution Time"],
                            "shared_hit_blocks": root.get("Shared Hit Blocks", 0),
                            "local_hit_blocks": root.get("Local Hit Blocks", 0),
                            "local_read_blocks": root.get("Local Read Blocks", 0),
                            "plan": plan,
                        }
                    )
            print(json.dumps({"fixture_rows": 100000, "same_result_ids": actual, "results": results}, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
