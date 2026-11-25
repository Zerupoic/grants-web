"""Build the grants webpage from the downloaded grants.gov CSVs.

- Compares the latest grants_data/grantsgov_*.csv with the previous one to split
  grants into "new this week" and "still open".
- Keeps grants open to higher-education applicants: applicant_types lists
  higher_education, or applicant_types is "other" and an LLM check of the
  eligibility description says eligible / needs further information.
- Writes site/index.html and site/grantsgov_latest.csv.

LLM settings come from the environment (any OpenAI-compatible API works):
  LLM_API_KEY   required for the LLM check
  LLM_BASE_URL  default https://api.deepseek.com  (OpenAI: https://api.openai.com/v1)
  LLM_MODEL     default deepseek-flash            (OpenAI: gpt-4o-mini)
Without LLM_API_KEY, "other" grants are kept and tagged "needs review" so the
page can still be built locally.
"""
import glob
import hashlib
import html
import json
import os
import re
import shutil
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "grants_data"
SITE_DIR = ROOT / "site"
TEMPLATE = ROOT / "templates" / "index.html"
CACHE_FILE = DATA_DIR / "eligibility_cache.json"

LLM_BASE_URL = os.getenv("LLM_BASE_URL") or "https://api.deepseek.com"
LLM_MODEL = os.getenv("LLM_MODEL") or "deepseek-flash"
LABELS = ("eligible", "not eligible", "need further information")
KEEP_LABELS = ("eligible", "need further information")
CLOSING_SOON_DAYS = 30

HIGHER_ED_RE = re.compile(r"(?<!non_)higher_education")


# ---------------------------------------------------------------- data loading

def load_csv(path) -> pd.DataFrame:
    """Read a grants CSV as strings, with snake_case column names."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [re.sub(r"\s+", "_", c.strip().lower()) for c in df.columns]
    return df


def split_new_old(df_latest: pd.DataFrame, df_prev: pd.DataFrame):
    col = "opportunity_number"
    if col not in df_latest.columns:
        return df_latest, df_latest.iloc[0:0]
    prev_ids = set()
    if col in df_prev.columns:
        prev_ids = set(df_prev[col].str.strip())
    is_new = ~df_latest[col].str.strip().isin(prev_ids)
    return df_latest[is_new], df_latest[~is_new]


# ------------------------------------------------------- eligibility filtering

class EligibilityJudge:
    """LLM eligibility check with an on-disk cache keyed by opportunity id."""

    PROMPT = """
You are reading a grant eligibility description.

Your task:
Decide whether Higher Education Institutions (such as universities, colleges, academic institutions) are eligible to apply.

Return ONLY one of these three labels:
eligible
not eligible
need further information

Eligibility description:
{text}
"""

    def __init__(self):
        self.cache = {}
        if CACHE_FILE.exists():
            self.cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        self.calls = 0
        self.client = None
        # DeepSeek models think before answering by default; a one-word label doesn't need it
        self.extra_body = {"thinking": {"type": "disabled"}} if "deepseek" in LLM_BASE_URL else None
        api_key = os.getenv("LLM_API_KEY")
        if api_key:
            from openai import OpenAI
            self.client = OpenAI(api_key=api_key, base_url=LLM_BASE_URL)
            print(f"LLM check: {LLM_MODEL} at {LLM_BASE_URL}")
        else:
            print("Warning: LLM_API_KEY is not set; 'other' grants will be kept as 'need further information'.")

    def judge(self, opp_id: str, text: str) -> str:
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        cached = self.cache.get(opp_id)
        if cached and cached.get("hash") == text_hash:
            return cached["label"]
        if self.client is None:
            return "need further information"

        label = self._ask(text)
        self.calls += 1
        self.cache[opp_id] = {"label": label, "hash": text_hash}
        time.sleep(0.5)  # avoid rate limits
        return label

    def _ask(self, text: str) -> str:
        try:
            response = self.client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": self.PROMPT.format(text=text)}],
                temperature=0,
                extra_body=self.extra_body,
            )
            raw = (response.choices[0].message.content or "").strip().lower().strip(" .\"'")
        except Exception as e:
            print(f"Error calling the LLM: {e}")
            return "need further information"
        # Check "not eligible" before "eligible" since one contains the other
        for label in ("not eligible", "need further information", "eligible"):
            if label in raw:
                return label
        return "need further information"

    def save(self):
        CACHE_FILE.write_text(json.dumps(self.cache, indent=2, sort_keys=True), encoding="utf-8")


def filter_grants(df: pd.DataFrame, judge: EligibilityJudge) -> list[dict]:
    """Return kept grants as dicts, each with an 'eligibility' tag."""
    kept = []
    for _, row in df.iterrows():
        grant = row.to_dict()
        applicant_types = grant.get("applicant_types", "").lower()
        if HIGHER_ED_RE.search(applicant_types):
            grant["eligibility"] = "higher-ed"
        elif "other" in applicant_types:
            opp_id = grant.get("opportunity_id") or grant.get("opportunity_number", "")
            label = judge.judge(opp_id, grant.get("applicant_eligibility_description", ""))
            if label not in KEEP_LABELS:
                continue
            grant["eligibility"] = "llm-eligible" if label == "eligible" else "llm-review"
        else:
            continue
        kept.append(grant)
    return kept


# ------------------------------------------------------------------- rendering

ELIGIBILITY_TAGS = {
    "higher-ed": ("Higher-ed listed", "Applicant types list higher education"),
    "llm-eligible": ("Eligible (AI check)", "Applicant type is 'other'; an AI read of the eligibility text says higher-ed can apply"),
    "llm-review": ("Needs review", "Applicant type is 'other'; eligibility for higher-ed is unclear from the text"),
}


def esc(value) -> str:
    return html.escape(str(value or "").strip())


def money(value: str) -> str:
    v = str(value or "").strip()
    try:
        n = float(v)
    except ValueError:
        return esc(v) or "—"
    return f"${n:,.0f}" if n > 0 else "—"


def plain_text(raw_html: str, limit: int = 1200) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def days_left(close_date: str, today: date):
    d = pd.to_datetime(close_date, errors="coerce")
    if pd.isna(d):
        return None
    return (d.date() - today).days


def link(url: str, label: str) -> str:
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return esc(url) or "—"
    return f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(label)}</a>'


def render_card(g: dict, today: date) -> str:
    title = g.get("opportunity_title") or "Untitled opportunity"
    url = g.get("url", "")
    left = days_left(g.get("close_date", ""), today)
    closing_soon = left is not None and 0 <= left <= CLOSING_SOON_DAYS

    badge = ""
    if closing_soon:
        badge = f'<span class="badge badge-soon">{"Closes today" if left == 0 else f"Closes in {left} day" + ("" if left == 1 else "s")}</span>'
    tag_label, tag_title = ELIGIBILITY_TAGS[g["eligibility"]]
    tag = f'<span class="badge badge-{g["eligibility"]}" title="{esc(tag_title)}">{esc(tag_label)}</span>'

    title_html = link(url, title) if str(url).startswith("http") else esc(title)
    agency = g.get("agency_name") or g.get("agency_code") or ""
    summary = plain_text(g.get("summary_description", ""))
    eligibility_text = plain_text(g.get("applicant_eligibility_description", ""), 600)

    search_blob = " ".join([
        title, g.get("opportunity_number", ""), agency,
        g.get("top_level_agency_name", ""), summary,
    ]).lower()

    details = ""
    if summary or eligibility_text:
        parts = []
        if summary:
            parts.append(f"<p>{esc(summary)}</p>")
        if eligibility_text:
            parts.append(f'<p><strong>Eligibility:</strong> {esc(eligibility_text)}</p>')
        details = f'<details><summary>Summary</summary>{"".join(parts)}</details>'

    return f"""
<article class="card{' card-soon' if closing_soon else ''}" data-search="{esc(search_blob)}" data-soon="{1 if closing_soon else 0}" data-elig="{g['eligibility']}">
  <div class="card-head">
    <h3>{title_html}</h3>
    <div class="badges">{badge}{tag}</div>
  </div>
  <p class="meta">{esc(g.get('opportunity_number'))} · {esc(agency)}</p>
  <dl>
    <div><dt>Posted</dt><dd>{esc(g.get('post_date')) or '—'}</dd></div>
    <div><dt>Closes</dt><dd>{esc(g.get('close_date')) or '—'}</dd></div>
    <div><dt>Total funding</dt><dd>{money(g.get('estimated_total_program_funding'))}</dd></div>
    <div><dt>Award ceiling</dt><dd>{money(g.get('award_ceiling'))}</dd></div>
    <div><dt>Expected awards</dt><dd>{esc(g.get('expected_number_of_awards')) or '—'}</dd></div>
    <div><dt>More info</dt><dd>{link(g.get('additional_info_url'), 'Agency page')}</dd></div>
  </dl>
  {details}
</article>"""


def sort_key(g: dict):
    d = pd.to_datetime(g.get("close_date", ""), errors="coerce")
    return (pd.isna(d), d if not pd.isna(d) else pd.Timestamp.max)


def render_section(section_id: str, title: str, note: str, grants: list[dict], today: date) -> str:
    cards = "".join(render_card(g, today) for g in sorted(grants, key=sort_key))
    empty_msg = "No grants in this group." if not grants else "No grants match your filters."
    return f"""
<section id="{section_id}" class="group">
  <div class="group-head">
    <h2>{esc(title)} <span class="count" data-total="{len(grants)}">{len(grants)}</span></h2>
    <p>{esc(note)}</p>
  </div>
  <div class="cards">{cards}</div>
  <p class="empty"{'' if not grants else ' hidden'}>{empty_msg}</p>
</section>"""


def build_page(new_grants, old_grants, latest_file: Path, prev_file) -> str:
    today = date.today()
    all_grants = new_grants + old_grants
    soon = sum(
        1 for g in all_grants
        if (d := days_left(g.get("close_date", ""), today)) is not None and 0 <= d <= CLOSING_SOON_DAYS
    )
    data_date = datetime.strptime(latest_file.stem.split("_")[-1], "%Y%m%d").strftime("%B %d, %Y")
    compared = (
        f"Compared with {datetime.strptime(Path(prev_file).stem.split('_')[-1], '%Y%m%d').strftime('%B %d, %Y')}"
        if prev_file else "No earlier download to compare with, so every grant counts as new"
    )

    slots = {
        "DATA_DATE": esc(data_date),
        "BUILT_AT": esc(datetime.now().strftime("%Y-%m-%d %H:%M")),
        "COMPARED": esc(compared),
        "TOTAL": str(len(all_grants)),
        "NEW_COUNT": str(len(new_grants)),
        "SOON_COUNT": str(soon),
        "SECTIONS": render_section(
            "new", "New this week", "Posted since the previous download.", new_grants, today
        ) + render_section(
            "open", "Still open", "Listed in the previous download too.", old_grants, today
        ),
    }
    page = TEMPLATE.read_text(encoding="utf-8")
    for key, value in slots.items():
        page = page.replace(f"{{{{{key}}}}}", value)
    return page


# ------------------------------------------------------------------------ main

def main():
    files = sorted(glob.glob(str(DATA_DIR / "grantsgov_*.csv")))
    if not files:
        raise SystemExit(f"No CSV files found in {DATA_DIR}")

    latest_file = Path(files[-1])
    prev_file = files[-2] if len(files) >= 2 else None
    df_latest = load_csv(latest_file)
    df_prev = load_csv(prev_file) if prev_file else pd.DataFrame()
    print(f"Latest: {latest_file.name} ({len(df_latest)} grants)")
    print(f"Previous: {Path(prev_file).name if prev_file else 'none'}")

    df_new, df_old = split_new_old(df_latest, df_prev)

    judge = EligibilityJudge()
    try:
        new_grants = filter_grants(df_new, judge)
        old_grants = filter_grants(df_old, judge)
    finally:
        judge.save()
    print(f"New grants kept: {len(new_grants)} of {len(df_new)}")
    print(f"Still-open grants kept: {len(old_grants)} of {len(df_old)}")
    print(f"LLM calls this run: {judge.calls}")

    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "index.html").write_text(
        build_page(new_grants, old_grants, latest_file, prev_file), encoding="utf-8"
    )
    shutil.copyfile(latest_file, SITE_DIR / "grantsgov_latest.csv")
    (SITE_DIR / ".nojekyll").touch()
    print(f"Site written to {SITE_DIR / 'index.html'}")


if __name__ == "__main__":
    main()
