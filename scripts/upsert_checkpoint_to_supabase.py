import os
import json
from urllib.parse import quote_plus
import psycopg2
from psycopg2.extras import execute_values

CHECKPOINT_JSON_PATH = os.getenv("CHECKPOINT_JSON_PATH", "data/parsed_questions_checkpoint.json")

DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "")
CONN_STR = f"postgresql://{DB_USER}:{quote_plus(DB_PASS)}@{DB_HOST}:{DB_PORT}/{DB_NAME}?sslmode=require"

DDL_SQL = """
CREATE TABLE IF NOT EXISTS public.questions (
    id BIGSERIAL PRIMARY KEY,
    question_hash TEXT UNIQUE NOT NULL,
    syllabus_unit_id TEXT NOT NULL,
    exams TEXT[] NOT NULL DEFAULT '{}',
    exam_year TEXT,
    key_stage TEXT NOT NULL DEFAULT 'TENTATIVE',
    question_en TEXT NOT NULL,
    options_en JSONB NOT NULL,
    correct_index SMALLINT CHECK (correct_index BETWEEN 0 AND 4),
    has_answer_key BOOLEAN NOT NULL DEFAULT FALSE,
    repeat_count INT NOT NULL DEFAULT 1,
    source_exam TEXT,
    source_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_questions_exams ON public.questions USING GIN(exams);
CREATE INDEX IF NOT EXISTS idx_questions_unit ON public.questions(syllabus_unit_id);
"""

UPSERT_SQL = """
INSERT INTO public.questions (
    question_hash, syllabus_unit_id, exams, exam_year, key_stage,
    question_en, options_en, correct_index, has_answer_key,
    repeat_count, source_exam, source_url
)
VALUES %s
ON CONFLICT (question_hash) DO UPDATE SET
    exams = ARRAY(SELECT DISTINCT unnest(public.questions.exams || EXCLUDED.exams)),
    repeat_count = GREATEST(public.questions.repeat_count, EXCLUDED.repeat_count),
    correct_index = COALESCE(EXCLUDED.correct_index, public.questions.correct_index),
    has_answer_key = public.questions.has_answer_key OR EXCLUDED.has_answer_key,
    key_stage = CASE WHEN EXCLUDED.key_stage = 'FINAL' THEN 'FINAL' ELSE public.questions.key_stage END;
"""

def main() -> None:
    if not os.path.exists(CHECKPOINT_JSON_PATH):
        raise FileNotFoundError(f"Checkpoint file not found: {CHECKPOINT_JSON_PATH}")

    with open(CHECKPOINT_JSON_PATH, "r", encoding="utf-8") as f:
        rows = json.load(f)

    if not isinstance(rows, list) or len(rows) == 0:
        raise ValueError(f"Checkpoint file is empty or invalid: {CHECKPOINT_JSON_PATH}")

    db_rows = [
        (
            r["question_hash"],
            r["syllabus_unit_id"],
            r["exams"],
            r.get("exam_year"),
            r.get("key_stage", "TENTATIVE"),
            r["question_en"],
            json.dumps(r["options_en"], ensure_ascii=False) if isinstance(r["options_en"], list) else r["options_en"],
            r.get("correct_index"),
            bool(r.get("has_answer_key", False)),
            int(r.get("repeat_count", 1)),
            r.get("source_exam"),
            r.get("source_url"),
        )
        for r in rows
    ]

    if not (DB_HOST and DB_PASS):
        raise RuntimeError("DB_HOST or DB_PASS secret is missing.")

    conn = psycopg2.connect(CONN_STR)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(DDL_SQL)
                execute_values(cur, UPSERT_SQL, db_rows, page_size=250)
        print(f"Upserted {len(db_rows)} questions from {CHECKPOINT_JSON_PATH} into Supabase!", flush=True)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
