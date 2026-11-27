import os
import re
import io
import sys
import time
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import urllib3
import requests
import pymupdf
from PIL import Image
import pytesseract
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
sys.stdout.reconfigure(line_buffering=True)

# ==========================================
# 1. CONFIGURATION
# ==========================================
BASE_URL = "https://www.tnpsc.gov.in/english/answerkeys.aspx"
WORK_DIR = os.getenv("WORK_DIR", "./tnpsc_full_pipeline")
PDF_DIR = os.path.join(WORK_DIR, "pdfs")
CHECKPOINT_FILE = os.path.join(WORK_DIR, "parsed_questions_checkpoint.json")
os.makedirs(PDF_DIR, exist_ok=True)

years_env = os.getenv("YEARS_TO_CRAWL", "0,2025,2024")
YEARS_TO_CRAWL = [y.strip() for y in years_env.split(",") if y.strip()]

INCLUDE_OTHER_EXAMS = os.getenv("INCLUDE_OTHER_EXAMS", "true").lower() == "true"

MAX_EXAM_ROWS_PER_YEAR = int(os.getenv("MAX_EXAM_ROWS_PER_YEAR", "10"))
MAX_PAGES_PER_PDF = int(os.getenv("MAX_PAGES_PER_PDF", "40"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "close",
}

SKIP_FILE_WORDS = [
    "check_list", "checklist", "instruction", "syllabus", "notification",
    "certificate", "faq", "rules", "regulation", "order", "notice", "candidates", "workshop"
]

OPTION_MAP = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
TICK_CHARS = ("✓", "✔", "☑", "", "")

# ==========================================
# 2. EXAM & YEAR CLASSIFICATION
# ==========================================
def classify_exams(title: str) -> list[str]:
    norm = re.sub(r"[–—−]", "-", title.lower())
    tags = []
    if "group - iv" in norm or "group-iv" in norm or "group iv" in norm or "vao" in norm:
        tags.append("GROUP_4")
    if "group - ii" in norm or "group-ii" in norm or "group ii" in norm:
        tags.append("GROUP_2")
        if "iia" in norm or "ii a" in norm:
            tags.append("GROUP_2A")
    if ("group - i " in norm or "group-i " in norm or "group i " in norm or "civil services examination - i" in norm) and "ii" not in norm and "iv" not in norm:
        tags.append("GROUP_1_PRELIMS")
    if not tags:
        tags.append("TNPSC_OTHER")
    return tags

def is_core_or_allowed_exam(tags: list[str]) -> bool:
    core = {"GROUP_1_PRELIMS", "GROUP_2", "GROUP_2A", "GROUP_4"}
    if set(tags) & core:
        return True
    return INCLUDE_OTHER_EXAMS

def extract_year_from_row(cols: list[str], fallback_year: str) -> str:
    joined = " ".join(cols)
    years = re.findall(r"\b(20\d{2})\b", joined)
    if years:
        return years[-1]
    return fallback_year

# ==========================================
# 3. CRAWL ANSWER KEYS PAGE BY YEAR
# ==========================================
def get_year_soup(session: requests.Session, year_val: str) -> BeautifulSoup | None:
    r = None
    for attempt in range(1, 4):
        try:
            r = session.get(BASE_URL, verify=False, timeout=60)
            r.raise_for_status()
            break
        except requests.RequestException as e:
            print(f"  [WARN] GET attempt {attempt}/3 for '{BASE_URL}' failed: {e}", flush=True)
            if attempt == 3:
                return None
            time.sleep(2 * attempt)

    if r is None:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    if not year_val or year_val == "0":
        return soup

    ddl = soup.find("select", {"name": re.compile(r"ddl_year$")})
    available_values = [
        opt.get("value", "").strip()
        for opt in (ddl.find_all("option") if ddl else [])
        if opt.get("value") is not None
    ]
    if available_values:
        print(f"  [INFO] Available ddl_year options: {available_values}", flush=True)
        if str(year_val) not in available_values:
            print(f"  [WARN] Year '{year_val}' not in ddl_year options; falling back to default view.", flush=True)
            return soup

    vs = soup.find("input", {"name": "__VIEWSTATE"})
    ev = soup.find("input", {"name": "__EVENTVALIDATION"})
    vsg = soup.find("input", {"name": "__VIEWSTATEGENERATOR"})
    if not (vs and ev):
        print(f"  [WARN] Missing ASP.NET form tokens for year '{year_val}'; falling back to default view.", flush=True)
        return soup

    field_name = ddl["name"] if ddl and ddl.get("name") else "ctl00$ContentPlaceHolder1$ddl_year"
    payload = {
        "__EVENTTARGET": field_name,
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": vs.get("value", ""),
        "__EVENTVALIDATION": ev.get("value", ""),
        field_name: str(year_val),
    }
    if vsg and vsg.get("value"):
        payload["__VIEWSTATEGENERATOR"] = vsg["value"]

    post_headers = {
        "Referer": BASE_URL,
        "Origin": "https://www.tnpsc.gov.in",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        rp = session.post(BASE_URL, data=payload, headers=post_headers, verify=False, timeout=60)
        rp.raise_for_status()
        return BeautifulSoup(rp.text, "html.parser")
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        print(f"  [WARN] POST for year '{year_val}' returned HTTP {status}; falling back to default view.", flush=True)
        return soup
    except requests.RequestException as e:
        print(f"  [WARN] POST for year '{year_val}' failed ({e}); falling back to default view.", flush=True)
        return soup

def crawl_all_selected_years() -> list[dict]:
    session = requests.Session()
    session.headers.update(HEADERS)
    downloaded_items = []

    for yr in YEARS_TO_CRAWL:
        print(f"\n================ CRAWLING YEAR FILTER: {yr} ================", flush=True)
        soup = get_year_soup(session, yr)
        if soup is None:
            print(f"  [WARN] Skipping year '{yr}' because TNPSC did not respond in time.", flush=True)
            continue

        content = soup.find(id="page-content") or soup

        rows_collected = 0
        for tr in content.select("table tr"):
            cols = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cols) < 3:
                continue
            row_txt = " ".join(cols)
            if "s no." in row_txt.lower() and "notification" in row_txt.lower():
                continue

            exam_title = cols[2] if len(cols) > 2 else row_txt[:120]
            exam_tags = classify_exams(exam_title)
            if not is_core_or_allowed_exam(exam_tags):
                continue

            links = [(a.get_text(" ", strip=True), urljoin(BASE_URL, a["href"])) for a in tr.find_all("a", href=True)]
            if not links:
                continue

            rows_collected += 1
            if rows_collected > MAX_EXAM_ROWS_PER_YEAR:
                break

            exam_year = extract_year_from_row(cols, yr)
            row_uid = hashlib.md5(f"{exam_title}_{links[0][1]}".encode()).hexdigest()[:10]
            print(f"[{yr} #{rows_collected}] {exam_title[:75]} | tags={exam_tags} | year={exam_year}", flush=True)

            for label, href in links:
                if "subjects.aspx" in href.lower():
                    stage = "FINAL" if "final" in href.lower() else "TENTATIVE"
                    try:
                        sub_soup = BeautifulSoup(session.get(href, verify=False, timeout=45).text, "html.parser")
                    except Exception as e:
                        print(f"  [ERR] subjects page {href}: {e}", flush=True)
                        continue

                    sub_area = sub_soup.find(id="page-content") or sub_soup
                    for a in sub_area.find_all("a", href=True):
                        pdf_url = urljoin(href, a["href"])
                        subj = a.get_text(" ", strip=True) or label
                        combo = (subj + " " + pdf_url).lower()
                        if ".pdf" not in combo or any(sw in combo for sw in SKIP_FILE_WORDS):
                            continue

                        fname = re.sub(r"[^a-zA-Z0-9_-]+", "_", f"{row_uid}_{subj[:40]}") + ".pdf"
                        fpath = os.path.join(PDF_DIR, fname)
                        if not os.path.exists(fpath):
                            try:
                                pr = session.get(pdf_url, verify=False, timeout=60)
                                if pr.status_code == 200 and pr.content[:4] == b"%PDF":
                                    with open(fpath, "wb") as f:
                                        f.write(pr.content)
                                else:
                                    continue
                                time.sleep(0.2)
                            except Exception as e:
                                print(f"  [ERR] {pdf_url}: {e}", flush=True)
                                continue

                        is_key = any(k in combo for k in ["fak", "final_key", "answerkey", "answer_key"])
                        downloaded_items.append({
                            "path": fpath,
                            "row_uid": row_uid,
                            "exam_title": exam_title,
                            "subject": subj,
                            "exams": exam_tags,
                            "exam_year": exam_year,
                            "key_stage": stage,
                            "source_url": pdf_url,
                            "is_key_sheet": is_key,
                        })
                        print(f"  [PDF] {fname}", flush=True)

                elif href.lower().endswith(".pdf"):
                    combo = (label + " " + href).lower()
                    if any(sw in combo for sw in SKIP_FILE_WORDS):
                        continue
                    fname = re.sub(r"[^a-zA-Z0-9_-]+", "_", f"{row_uid}_{label[:40]}") + ".pdf"
                    fpath = os.path.join(PDF_DIR, fname)
                    if not os.path.exists(fpath):
                        try:
                            pr = session.get(href, verify=False, timeout=60)
                            if pr.status_code == 200 and pr.content[:4] == b"%PDF":
                                with open(fpath, "wb") as f:
                                    f.write(pr.content)
                            else:
                                continue
                        except Exception as e:
                            print(f"  [ERR] {href}: {e}", flush=True)
                            continue

                    is_key = any(k in combo for k in ["fak", "final_key", "answerkey", "answer_key"])
                    downloaded_items.append({
                        "path": fpath,
                        "row_uid": row_uid,
                        "exam_title": exam_title,
                        "subject": label,
                        "exams": exam_tags,
                        "exam_year": exam_year,
                        "key_stage": "FINAL" if is_key else "TENTATIVE",
                        "source_url": href,
                        "is_key_sheet": is_key,
                    })
                    print(f"  [PDF] {fname} (key_sheet={is_key})", flush=True)

    return downloaded_items

# ==========================================
# 4. ANSWER KEY MAP & ENGLISH QUESTION PARSER
# ==========================================
def build_key_maps_by_row(key_items: list[dict]) -> dict[str, dict[int, int]]:
    maps: dict[str, dict[int, int]] = {}
    for item in key_items:
        doc = pymupdf.open(item["path"])
        try:
            txt = "\n".join(p.get_text("text") for p in doc)
        finally:
            doc.close()
        qmap = maps.setdefault(item["row_uid"], {})
        for m in re.finditer(r"\b([1-9]|[1-9][0-9]|1[0-9]{2}|200)\s*[\.\-\):]\s*\(?([A-Ea-e])\)?", txt):
            qmap[int(m.group(1))] = OPTION_MAP[m.group(2).upper()]
    return maps

def clean_english_line(line: str) -> str:
    s = line.replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "").strip()
    if not s or len(s) <= 2:
        return ""
    if sum(1 for ch in s if "\u0B80" <= ch <= "\u0BFF") > 2:
        return ""
    if any(tok in s for tok in ["விடைதெரியவில்லை", "[Turn over"]):
        return ""
    if re.match(r"^[A-Z0-9\-/]+\s+[A-E]\s+\d{1,3}$", s):
        return ""
    return re.sub(r"\s+", " ", s)

def classify_unit(q_en: str, subject: str) -> str:
    t = (q_en + " " + subject).lower()
    if any(k in t for k in ["article", "constitution", "parliament", "fundamental", "governor", "supreme court", "panchayat", "president", "amendment"]):
        return "GS_POLITY"
    if any(k in t for k in ["thirukkural", "sangam", "chola", "pallava", "pandya", "periyar", "justice party", "tamil nadu", "madras"]):
        return "GS_UNIT8_TN_HISTORY"
    if any(k in t for k in ["gdp", "inflation", "rbi", "budget", "tax", "five year", "human development", "economy", "fiscal"]):
        return "GS_ECONOMY_UNIT9"
    if any(k in t for k in ["river", "monsoon", "soil", "latitude", "national park", "mountain", "climate"]):
        return "GS_GEOGRAPHY"
    if any(k in t for k in ["ratio", "percentage", "interest", "lcm", "hcf", "probability", "average", "speed", "reasoning", "aptitude"]):
        return "GS_APTITUDE"
    return "GS_GENERAL"

def _ocr_single_page(args: tuple[str, int, int]) -> str:
    pdf_path, page_idx, dpi = args
    doc = pymupdf.open(pdf_path)
    try:
        pix = doc[page_idx].get_pixmap(dpi=dpi)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        return pytesseract.image_to_string(img, lang="eng", config="--psm 6")
    finally:
        doc.close()

def extract_booklet_english_text(pdf_path: str, max_pages: int = 40) -> str:
    doc = pymupdf.open(pdf_path)
    total_pages = len(doc)
    digital_sample = "\n".join(doc[i].get_text("text") for i in range(min(4, total_pages))).strip()
    use_digital = len(digital_sample) > 400
    limit = min(total_pages, max_pages)

    raw_pages: list[str] = []
    if use_digital:
        for idx in range(limit):
            raw_pages.append(doc[idx].get_text("text"))
        doc.close()
    else:
        doc.close()
        tasks = [(pdf_path, idx, 150) for idx in range(limit)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            raw_pages = list(pool.map(_ocr_single_page, tasks))

    out_pages = []
    for raw in raw_pages:
        if not raw or len(raw.strip()) < 100 or "SPACE FOR ROUGH WORK" in raw.upper():
            continue
        cleaned_lines = [cl for ln in raw.splitlines() if (cl := clean_english_line(ln))]
        if len("\n".join(cleaned_lines)) > 50:
            out_pages.append("\n".join(cleaned_lines))

    return "\n".join(out_pages)

def parse_questions_from_text(text: str, meta: dict, key_map: dict[int, int]) -> list[dict]:
    blocks = re.split(r"\n(?=(?:[1-9]|[1-9][0-9]|1[0-9]{2}|200)\s*[\.\)]\s*)", text)
    parsed = []

    for block in blocks:
        m_num = re.match(r"^([1-9]|[1-9][0-9]|1[0-9]{2}|200)\s*[\.\)]\s*(.*)", block.strip(), re.S)
        if not m_num:
            continue
        q_num = int(m_num.group(1))
        body = m_num.group(2)

        markers = list(re.finditer(r"(?:^|\s)[\(\[]?([A-Ea-e0])[\)\.]\s+", body))
        if len(markers) < 4:
            continue

        q_en = re.sub(r"\s+", " ", body[:markers[0].start()]).strip()
        slots = {k: [] for k in ["A", "B", "C", "D", "E"]}
        tick_idx = None

        for idx, mk in enumerate(markers):
            raw_lbl = mk.group(1).upper()
            lbl = "C" if raw_lbl == "0" else raw_lbl
            seg = body[mk.end(): markers[idx + 1].start() if idx + 1 < len(markers) else len(body)].strip()

            if any(tc in seg for tc in TICK_CHARS) and tick_idx is None and lbl in OPTION_MAP:
                tick_idx = OPTION_MAP[lbl]
            for tc in TICK_CHARS:
                seg = seg.replace(tc, "")

            seg = re.split(r"\bAnswer not known\b", seg, maxsplit=1, flags=re.I)[0]
            if lbl == "E" and not seg.strip():
                seg = "Answer not known"

            if lbl in slots:
                cleaned_seg = re.sub(r"\s+", " ", seg).strip()
                if cleaned_seg:
                    slots[lbl].append(cleaned_seg)

        options_en = [slots[k][0] for k in ["A", "B", "C", "D", "E"] if slots[k]]
        if len(options_en) < 4 or not q_en:
            continue

        correct_idx = key_map.get(q_num, tick_idx)
        has_key = correct_idx in (0, 1, 2, 3, 4)

        fp = re.sub(r"[^a-z0-9]+", "", q_en.lower())[:160]
        q_hash = hashlib.sha256(fp.encode("utf-8")).hexdigest()[:24]

        parsed.append({
            "question_hash": q_hash,
            "syllabus_unit_id": classify_unit(q_en, meta["subject"]),
            "exams": meta["exams"],
            "exam_year": meta["exam_year"],
            "key_stage": meta["key_stage"],
            "question_en": q_en,
            "options_en": options_en,
            "correct_index": int(correct_idx) if has_key else None,
            "has_answer_key": has_key,
            "source_exam": meta["exam_title"],
            "source_url": meta["source_url"],
        })

    return parsed

# ==========================================
# 5. BATCHED EXTRACTION (EVERY 5 PDFs) & JSON SAVE
# ==========================================
def load_existing_checkpoint(path: str) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        if isinstance(existing, list):
            return {item["question_hash"]: item for item in existing if item.get("question_hash")}
    except Exception as e:
        print(f"[WARN] Could not load existing checkpoint {path}: {e}", flush=True)
    return {}

def merge_questions_into_map(merged: dict[str, dict], new_items: list[dict]) -> dict[str, dict]:
    for item in new_items:
        h = item["question_hash"]
        if h in merged:
            existing_exams = merged[h].get("exams", [])
            merged[h]["exams"] = sorted(list(set(existing_exams + item.get("exams", []))))
            merged[h]["repeat_count"] = int(merged[h].get("repeat_count", 1)) + 1
            if item.get("correct_index") is not None:
                merged[h]["correct_index"] = item["correct_index"]
                merged[h]["has_answer_key"] = True
                if item.get("key_stage") == "FINAL":
                    merged[h]["key_stage"] = "FINAL"
        else:
            copy_item = dict(item)
            copy_item["repeat_count"] = int(copy_item.get("repeat_count", 1))
            merged[h] = copy_item
    return merged

def write_checkpoint_json(merged: dict[str, dict], out_path: str) -> list[dict]:
    final_list = list(merged.values())
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(final_list, f, ensure_ascii=False, indent=2)
    return final_list

def main() -> None:
    all_downloads = crawl_all_selected_years()
    key_sheets = [d for d in all_downloads if d["is_key_sheet"]]
    booklets = [d for d in all_downloads if not d["is_key_sheet"]]
    row_key_maps = build_key_maps_by_row(key_sheets)

    merged = load_existing_checkpoint(CHECKPOINT_FILE)
    print(
        f"\nLoaded {len(merged)} existing questions from checkpoint. "
        f"Found {len(key_sheets)} key sheets and {len(booklets)} booklets "
        f"(batch_size={BATCH_SIZE}).",
        flush=True,
    )

    final_list = write_checkpoint_json(merged, CHECKPOINT_FILE)
    total_batches = (len(booklets) + BATCH_SIZE - 1) // BATCH_SIZE if booklets else 0

    for batch_num in range(1, total_batches + 1):
        start_idx = (batch_num - 1) * BATCH_SIZE
        batch_booklets = booklets[start_idx : start_idx + BATCH_SIZE]
        t0 = time.time()
        batch_questions = []

        print(
            f"\n--- Batch {batch_num}/{total_batches}: processing {len(batch_booklets)} PDFs ---",
            flush=True,
        )
        for b in batch_booklets:
            txt = extract_booklet_english_text(b["path"], max_pages=MAX_PAGES_PER_PDF)
            qs = parse_questions_from_text(txt, b, row_key_maps.get(b["row_uid"], {}))
            batch_questions.extend(qs)
            print(
                f"  Parsed {len(qs)} questions from {os.path.basename(b['path'])}",
                flush=True,
            )

        merged = merge_questions_into_map(merged, batch_questions)
        final_list = write_checkpoint_json(merged, CHECKPOINT_FILE)
        print(
            f"Updated {CHECKPOINT_FILE} after Batch {batch_num}/{total_batches}: "
            f"+{len(batch_questions)} parsed, {len(final_list)} unique total ({time.time() - t0:.1f}s)",
            flush=True,
        )

    print(f"\nTotal unique English questions ready: {len(final_list)}", flush=True)
    print(
        f"With answer key: {sum(1 for x in final_list if x.get('has_answer_key'))} | "
        f"Awaiting key: {sum(1 for x in final_list if not x.get('has_answer_key'))}",
        flush=True,
    )

if __name__ == "__main__":
    main()
