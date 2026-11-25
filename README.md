# grants-web

Every week this downloads the grants.gov (simpler.grants.gov, DOT search) export and keeps the grants that higher-education institutions can apply for. It publishes them as a webpage on GitHub Pages. It replaces the email report from `auto-email-test-main` and drops the sam.gov part.

## How it works

| Step | File | What it does |
|---|---|---|
| 1 | `fetch_grantgov.py` | Opens the grants.gov search in headless Chromium, clicks "Export results as .csv", and saves `grants_data/grantsgov_YYYYMMDD.csv` |
| 2 | `build_site.py` | Compares the newest CSV with the one before it (new vs. still open). It keeps grants whose applicant types list higher education. For applicant type "other", it asks an LLM (DeepSeek `deepseek-flash` by default) to read the eligibility text. Then it writes `site/index.html` |
| 3 | `.github/workflows/update_site.yml` | Runs steps 1–2 every Monday, commits the new CSV back to the repo, and deploys `site/` to GitHub Pages |

LLM answers are cached in `grants_data/eligibility_cache.json`, keyed by opportunity id. Each grant is judged once, and judged again only if its eligibility text changes.

### LLM settings

Any OpenAI-compatible API works. Settings are read from environment variables:

| Variable | Default | OpenAI example |
|---|---|---|
| `LLM_API_KEY` | (none: "other" grants are tagged Needs review) | your OpenAI key |
| `LLM_BASE_URL` | `https://api.deepseek.com` | `https://api.openai.com/v1` |
| `LLM_MODEL` | `deepseek-flash` | `gpt-4o-mini` |

On GitHub, `LLM_API_KEY` is a repository **secret**. `LLM_BASE_URL` and `LLM_MODEL` are optional repository **variables** (Settings → Secrets and variables → Actions → Variables).

## One-time GitHub setup

1. Create a GitHub repository and push this folder to it.
2. In the repository, open **Settings → Pages** and set **Source** to **GitHub Actions**.
3. Open **Settings → Secrets and variables → Actions** and add a secret named `LLM_API_KEY` (your DeepSeek key).
4. Open **Actions → Update grants site → Run workflow** to publish the first version. The page URL shows on the run summary, usually `https://<user>.github.io/<repo>/`.

## Running locally

```powershell
pip install -r requirements.txt
playwright install chromium
python fetch_grantgov.py
$env:LLM_API_KEY = "sk-..."   # optional, DeepSeek key
python build_site.py
start site\index.html
```

Without `LLM_API_KEY`, grants with applicant type "other" are still kept, tagged **Needs review**.
