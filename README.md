# NCLAT Cause List — OneCourt

`onecourt.in/nclat/causelist/` — the NCLAT daily cause lists as a searchable web page instead of separate PDFs.

## How it works

```
GitHub Actions (cron)  ──►  scrape.py  ──►  data/*.json  ──►  index.html (on onecourt.in)
   every 30 min eve/night      read listing,     committed to       reads the JSON from
                               fetch PDFs,       this repo          raw.githubusercontent.com
                               parse
```

No server. nclat.nic.in doesn't send CORS headers, so the browser can't read its PDFs directly — the scraper does it on a schedule instead.

### Source

`https://nclat.nic.in/daily-cause-list?page=N` is a paginated Drupal table of (court, description, date, PDF link). **File names are not predictable** (`Causelist_II_14.10.2026.pdf`, `Supp_Causelist_IV_13.10.2026.pdf`, `14.10.2026.pdf` for Chennai, `Registrar Court_13.10.2026.pdf`, `… Suppl (II).pdf`), so the scraper reads the listing instead of guessing URLs. A missing file returns 404.

| Listing court | Key | Shown as |
|---|---|---|
| Chairperson Court | `ch` | Chairperson |
| Court - 2 | `II` | Court-II |
| Court - 4 | `IV` | Court-IV |
| Chennai Bench | `chennai` | Chennai |
| Registrar Court | `reg` | Registrar |

Each court has a main list and, often, one or more supplementary lists per date ("Supplementary", "Supplementary (2)", "(II)"). On the page, each court shows its supplementary lists first, in order, then the main list.

Runs 9:00 AM – 11:00 PM IST (every 30 min morning and evening, hourly midday), none overnight. Each run checks 2 days back to 7 days ahead. Unchanged PDFs are skipped by ETag/hash, so a quiet run commits nothing. Dates older than 60 days are pruned.

### Parsing

The PDFs are tables — S.No | Case No | Parties | Counsel for Appellants | Counsel for Respondents (the Registrar list has no respondents column). `parse_pdf()` reads lines by x-position with PyMuPDF. Column edges are found from the body of each PDF (item numbers, then the sharp peaks of line start positions) rather than the header row, because header cells are centred in some courts' PDFs.

Known limits: advocate names that wrap onto a second line can occasionally be split in two; about 0.2% of matters with multi-part party blocks (e.g. "… WITH …") have no respondent split and show everything under the first party.

### JSON shape (`data/<date>.json`)

```
{ date, fetched, lists: [{name, ckey, court, order, kind: main|supp, seq, label, url, published, items}],
  benches: [{ list, ckey, court, time, judges, vc, notes[],
              sections: [{ t: "For Admission (Fresh Cases)", items: [
                { n: "12", c: case no., p: appellant, r: respondent,
                  pa: appellant's counsel, ra: respondent's counsel, x: [notes/IAs] } ] }] }] }
```

## Checking the parse

```bash
python3 validate.py        # items with no case no./party; gaps in item numbering
```

## Running locally

```bash
pip install -r requirements.txt
python3 scrape.py                          # refresh data/
python3 scrape.py --date 2026-10-13 --force
python3 scrape.py --parse some.pdf         # parse one PDF, print JSON
python3 -m http.server 8772                # then open http://localhost:8772/
```

On localhost the page reads `data/`; anywhere else it reads this repo's `data/` from `raw.githubusercontent.com` (see `DATA` in `index.html`).

## Deploying

- **Data:** this repo is `github.com/onecourtin/nclat-cause-list` (public — Actions minutes are free and raw files are readable). The workflow runs on its own; trigger it once by hand from the Actions tab.
- **Page:** upload `index.html` to `public_html/nclat/causelist/index.html` on the host. Only that one file goes there; the scraper, workflow and data stay on GitHub.
