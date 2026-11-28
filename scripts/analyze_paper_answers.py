import os
import re
import io
import json
import time
from collections import defaultdict
import requests

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None


def sanitize_url(raw_url: str, fallback: str = "") -> str:
    s = (raw_url or "").strip().strip('"').strip("'")
    md_match = re.match(r"^\[[^\]]*\]\((https?://[^)\s]+)\)$", s)
    if md_match:
        s = md_match.group(1).strip()
    elif s.startswith("[") and "](" in s and s.endswith(")"):
        inner = s.split("](", 1)[1][:-1].strip()
        if inner.startswith(("http://", "https://")):
            s = inner
    if not s and fallback:
        s = fallback
    return s.rstrip("/")


SUPABASE_URL = sanitize_url(os.getenv("SUPABASE_URL", ""))
SERVICE_KEY = os.getenv("SUPABASE_KEY", "").strip()

LLM_API_URL = sanitize_url(
    os.getenv("LLM_API_URL", ""),
    fallback="https://api.groq.com/openai/v1/chat/completions",
)
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b").strip()

SOURCE_URL_FILTER = os.getenv("SOURCE_URL_FILTER", "").strip()
EXAM_TAG_FILTER = os.getenv("EXAM_TAG_FILTER", "").strip()
BATCH_SIZE = int(os.getenv("ANALYZE_BATCH_SIZE", "8"))
MAX_QUESTIONS = int(os.getenv("MAX_ANALYZE_QUESTIONS", "200"))
CHECK_PDF_TICKS = os.getenv("CHECK_PDF_TICKS", "true").strip().lower() in ("1", "true", "yes")

TICK_CHARS = ("✓", "✔", "☑", "√", "☒")
_PDF_CACHE: dict[str, str] = {}


def sb_headers() -> dict[str, str]:
    return {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def parse_options(raw_opts) -> list[str]:
    if isinstance(raw_opts, list):
        return [str(x) for x in raw_opts]
    if isinstance(raw_opts, str):
        try:
            data = json.loads(raw_opts)
            if isinstance(data, list):
                return [str(x) for x in data]
        except Exception:
            pass
    return []


def extract_pdf_tick_snippets(source_url: str) -> str:
    if not CHECK_PDF_TICKS or PdfReader is None:
        return ""
    if not source_url.lower().endswith(".pdf") or not source_url.startswith(("http://", "https://")):
        return ""
    if source_url in _PDF_CACHE:
        return _PDF_CACHE[source_url]

    try:
        resp = requests.get(
            source_url,
            timeout=25,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TNPSC-Analyzer/1.0)"},
        )
        if not resp.ok or not resp.content:
            _PDF_CACHE[source_url] = ""
            return ""

        reader = PdfReader(io.BytesIO(resp.content))
        hits: list[str] = []
        for page_num, page in enumerate(reader.pages[:40], start=1):
            txt = page.extract_text() or ""
            for line in txt.splitlines():
                clean_line = " ".join(line.split())
                if any(ch in clean_line for ch in TICK_CHARS) or re.search(
                    r"\b(ans|answer|key|correct)\b\s*[:\-]?\s*\(?[a-e1-5]\)?",
                    clean_line,
                    re.IGNORECASE,
                ):
                    hits.append(f"P{page_num}: {clean_line[:140]}")
                    if len(hits) >= 40:
                        break
            if len(hits) >= 40:
                break

        summary = " | ".join(hits)
        _PDF_CACHE[source_url] = summary
        return summary
    except Exception:
        _PDF_CACHE[source_url] = ""
        return ""


def fetch_unanswered_by_paper(limit: int) -> dict[str, list[dict]]:
    query = (
        f"{SUPABASE_URL}/rest/v1/questions"
        f"?select=id,question_hash,question_en,options_en,correct_index,has_answer_key,syllabus_unit_id,source_exam,source_url,exams"
        f"&correct_index=is.null"
        f"&order=id.asc"
        f"&limit={limit}"
    )
    if EXAM_TAG_FILTER:
        clean_tag = EXAM_TAG_FILTER.strip("{}").strip()
        query += f"&exams=cs.{{{requests.utils.quote(clean_tag, safe='')}}}"
    if SOURCE_URL_FILTER:
        query += f"&source_url=eq.{requests.utils.quote(SOURCE_URL_FILTER, safe='')}"

    resp = requests.get(query, headers=sb_headers(), timeout=60)
    resp.raise_for_status()
    rows = resp.json()

    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        opts = parse_options(r.get("options_en"))
        if not opts:
            continue
        r["_parsed_options"] = opts
        grouped[r.get("source_url") or "UNKNOWN_PAPER"].append(r)
    return grouped


def extract_json_array(raw_content: str) -> list[dict]:
    text = raw_content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, list) else []
        raise


def analyze_paper_batch(source_url: str, paper_rows: list[dict], pdf_tick_hints: str) -> list[dict]:
    items = [
        {
            "hash": r["question_hash"],
            "question": r["question_en"],
            "options": [
                {"index": idx, "text": opt_text}
                for idx, opt_text in enumerate(r["_parsed_options"])
            ],
        }
        for r in paper_rows
    ]

    system_msg = (
        "You are an expert TNPSC exam solver. Determine the single best option index for each question "
        "by evaluating every option in 'options' against 'question' and any PDF tick/answer hints provided. "
        "Tolerate OCR typos or stray symbols in option text and infer the intended option. "
        "Avoid picking 'Answer not known' unless the question is unreadable or no other option fits. "
        "Keep 'reason' concise (under 25 words). "
        "Return ONLY a JSON array of objects: "
        '[{"hash": "...", "correct_index": 0, "confidence": 0.9, "reason": "..."}]'
    )

    hint_block = (
        f"\nPDF Tick/Answer Hints:\n{pdf_tick_hints}\n"
        if pdf_tick_hints
        else "\nPDF Tick/Answer Hints: None extracted (rely on option & subject analysis).\n"
    )
    user_msg = (
        f"Question Paper URL: {source_url}\n"
        f"{hint_block}\n"
        "Questions:\n" + json.dumps(items, ensure_ascii=False)
    )

    resp = None
    for attempt in range(1, 5):
        resp = requests.post(
            LLM_API_URL,
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
            },
            timeout=120,
        )
        if resp.status_code == 429:
            wait_s = 15 * attempt
            print(f"  [RATE LIMIT 429] Waiting {wait_s}s before retry {attempt}/4...", flush=True)
            time.sleep(wait_s)
            continue
        resp.raise_for_status()
        break

    if resp is None or resp.status_code == 429:
        raise RuntimeError("Rate limit (429) persisted after retries; lower ANALYZE_BATCH_SIZE.")

    raw_content = resp.json()["choices"][0]["message"]["content"]
    return extract_json_array(raw_content)


def patch_answer(question_hash: str, correct_index: int, reason: str) -> None:
    url = f"{SUPABASE_URL}/rest/v1/questions?question_hash=eq.{question_hash}&correct_index=is.null"
    headers = {**sb_headers(), "Prefer": "return=minimal"}
    body = {
        "correct_index": int(correct_index),
        "has_answer_key": True,
        "explanation_en": reason[:1000] if reason else None,
    }
    resp = requests.patch(url, headers=headers, json=body, timeout=60)
    if resp.status_code == 400 and "explanation_en" in resp.text:
        fallback_body = {
            "correct_index": int(correct_index),
            "has_answer_key": True,
        }
        resp = requests.patch(url, headers=headers, json=fallback_body, timeout=60)
    if not resp.ok:
        raise RuntimeError(f"Supabase PATCH failed ({resp.status_code}): {resp.text}")


def main() -> None:
    if not SUPABASE_URL.startswith(("http://", "https://")) or not SERVICE_KEY:
        raise RuntimeError("Valid SUPABASE_URL and SUPABASE_KEY secrets are required.")
    if not LLM_API_URL.startswith(("http://", "https://")):
        raise ValueError(f"Invalid LLM_API_URL: {LLM_API_URL!r}")
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY secret is required.")

    print(
        f"Using model: {LLM_MODEL} | exam_tag={EXAM_TAG_FILTER or 'ALL'} | batch_size={BATCH_SIZE} | max_questions={MAX_QUESTIONS} | pdf_ticks={CHECK_PDF_TICKS}",
        flush=True,
    )
    grouped_papers = fetch_unanswered_by_paper(MAX_QUESTIONS)
    if not grouped_papers:
        print("No remaining unanswered questions found matching filter.", flush=True)
        return

    total_updated = 0
    for paper_url, q_list in grouped_papers.items():
        pdf_hints = extract_pdf_tick_snippets(paper_url)
        print(
            f"\n=== Analyzing paper: {paper_url} ({len(q_list)} remaining unanswered | pdf_hints={'yes' if pdf_hints else 'none'}) ===",
            flush=True,
        )
        for i in range(0, len(q_list), BATCH_SIZE):
            chunk = q_list[i : i + BATCH_SIZE]
            by_hash = {q["question_hash"]: q for q in chunk}
            predictions = analyze_paper_batch(paper_url, chunk, pdf_hints)

            for pred in predictions:
                h = pred.get("hash")
                idx = pred.get("correct_index")
                q_obj = by_hash.get(h)
                if not q_obj:
                    continue
                max_idx = len(q_obj["_parsed_options"]) - 1
                if isinstance(idx, int) and 0 <= idx <= max_idx:
                    patch_answer(h, idx, str(pred.get("reason", "")))
                    total_updated += 1
                    print(
                        f"  [OK] {h} -> index {idx} ({q_obj['_parsed_options'][idx][:50]})",
                        flush=True,
                    )
            time.sleep(4.0)

    print(f"\nCompleted paper analysis. Updated {total_updated} questions in Supabase.", flush=True)


if __name__ == "__main__":
    main()
