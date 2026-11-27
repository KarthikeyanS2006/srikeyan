import os
import json
import requests

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SERVICE_KEY = os.getenv("SUPABASE_KEY", "").strip()
CHECKPOINT_JSON_PATH = os.getenv("CHECKPOINT_JSON_PATH", "data/parsed_questions_checkpoint.json")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "250"))


def normalize_options(val):
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def main() -> None:
    if not SUPABASE_URL or not SERVICE_KEY:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY environment variable/secret.")

    if not os.path.exists(CHECKPOINT_JSON_PATH):
        raise FileNotFoundError(f"Checkpoint file not found: {CHECKPOINT_JSON_PATH}")

    with open(CHECKPOINT_JSON_PATH, "r", encoding="utf-8") as f:
        rows = json.load(f)

    if not isinstance(rows, list) or len(rows) == 0:
        raise ValueError(f"No rows found in {CHECKPOINT_JSON_PATH}")

    payload = [
        {
            "question_hash": r["question_hash"],
            "syllabus_unit_id": r["syllabus_unit_id"],
            "exams": r.get("exams") or ["TNPSC_OTHER"],
            "exam_year": r.get("exam_year"),
            "key_stage": r.get("key_stage", "TENTATIVE"),
            "question_en": r["question_en"],
            "options_en": normalize_options(r.get("options_en")),
            "correct_index": r.get("correct_index"),
            "has_answer_key": bool(r.get("has_answer_key", False)),
            "repeat_count": int(r.get("repeat_count", 1)),
            "source_exam": r.get("source_exam"),
            "source_url": r.get("source_url"),
        }
        for r in rows
        if r.get("question_hash") and r.get("question_en")
    ]

    endpoint = f"{SUPABASE_URL}/rest/v1/questions?on_conflict=question_hash"
    headers = {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    total_sent = 0
    for i in range(0, len(payload), BATCH_SIZE):
        chunk = payload[i : i + BATCH_SIZE]
        resp = requests.post(endpoint, headers=headers, json=chunk, timeout=60)
        resp.raise_for_status()
        total_sent += len(chunk)
        print(f"Upserted {total_sent}/{len(payload)} questions to Supabase...", flush=True)

    print(f"Done! Upserted {total_sent} questions into public.questions.", flush=True)


if __name__ == "__main__":
    main()
