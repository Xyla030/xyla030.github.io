#!/usr/bin/env python3
"""Refresh the publications list in index.html from Google Scholar.

- Paper list, venue, year and citation counts come from the Scholar profile.
- Full author lists, abstracts and DOI links are filled in from OpenAlex
  (free API) by matching titles.
- The result is written as plain HTML between the PUBS:START / PUBS:END
  markers in index.html, so the site needs no JavaScript.

If Scholar blocks the request, the script leaves index.html untouched and
exits 0, so the scheduled GitHub Action just tries again next week.

Standard library only:  python3 update_pubs.py
"""
import datetime
import difflib
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request

SCHOLAR_ID = "ajsJrbsAAAAJ"
# How your name appears in author lists; matches get bolded.
MY_NAMES = ["XA Guo", "X Guo", "Xiaoyan Alex Guo", "X. Alex Guo", "Alex Guo",
            "Xiaoyan Guo", "X. A. Guo"]
# Co-first (equal contribution) authors. Scholar doesn't record this, so list
# it here. Key: a few words from the paper title. Value: part of each co-first
# author's name as it appears on the page (a last name is usually enough;
# use "me" for yourself). These authors get a * and a footnote.
EQUAL_CONTRIB = {
    "Population-Scale Precision Safety": ["Bakouny", "me"],
    "Semi-deconvolution of bulk and single-cell RNA-seq data": ["Lei", "me"],
    "Pan-cancer discovery of clinical and genomic determinants": ["Bakouny", "me"],
}
# Conference abstracts are detected automatically (e.g. "Abstract 1477 ...",
# venues like "Cancer Research 86 (7_Supplement)" or pages "S669-S670").
# Only abstracts where you are first (or co-first) author are listed.
# If something is sorted wrong, override it here by title words:
#   "paper" = list as a full paper, "abstract" = treat as abstract,
#   "hide" = never list.
FORCE_TYPE = {
    # "Some title words": "paper",
}
# Co-corresponding authors, same format as EQUAL_CONTRIB. They get a †.
CO_CORRESPONDING = {
    # "Some title words": ["Reznik", "Tansey"],
}
INDEX = "index.html"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 4:
                raise
            time.sleep(2 ** attempt * 2)  # rate-limited: wait 2s, 4s, 8s, 16s


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def scrape_scholar():
    url = (f"https://scholar.google.com/citations?user={SCHOLAR_ID}"
           "&hl=en&pagesize=100&sortby=pubdate")
    page = get(url)
    rows = re.findall(r'<tr class="gsc_a_tr">(.*?)</tr>', page, re.S)
    pubs = []
    for row in rows:
        title = re.search(r'class="gsc_a_at">(.*?)</a>', row, re.S)
        grays = re.findall(r'<div class="gs_gray">(.*?)</div>', row, re.S)
        year = re.search(r'class="gsc_a_h[^"]*">(\d{4})<', row)
        cites = re.search(r'class="gsc_a_ac[^"]*">(\d+)<', row)
        link = re.search(r'href="(/citations\?view_op=view_citation[^"]+)"', row)
        if not title:
            continue
        venue = grays[1] if len(grays) > 1 else ""
        venue = re.sub(r'<span class="gs_oph">.*?</span>', "", venue)
        pubs.append({
            "title": strip_tags(title.group(1)),
            "title_html": html.unescape(title.group(1)).strip(),
            "authors": strip_tags(grays[0]) if grays else "",
            "venue": strip_tags(venue),
            "year": int(year.group(1)) if year else None,
            "cites": int(cites.group(1)) if cites else 0,
            "scholar_url": "https://scholar.google.com" + html.unescape(link.group(1)) if link else "",
        })
    if not pubs:
        raise RuntimeError("no publications parsed (blocked or page changed)")
    return pubs


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower())


CACHE = "pubs_cache.json"  # last good OpenAlex details per paper
ENRICHED = ("authors", "abstract", "doi", "pdf")


def load_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def openalex_enrich(p):
    """Fill in authors/abstract/doi/pdf from OpenAlex. Returns True on a match."""
    q = urllib.parse.quote(p["title"][:250])
    try:
        data = json.loads(get(f"https://api.openalex.org/works?search={q}&per-page=5"))
    except Exception as e:  # network hiccup: just skip enrichment
        print("  openalex error:", e, file=sys.stderr)
        return False
    for w in data.get("results", []):
        ratio = difflib.SequenceMatcher(None, norm(w.get("display_name") or ""), norm(p["title"])).ratio()
        if ratio < 0.9:
            continue
        names = [a["author"]["display_name"] for a in w.get("authorships", [])]
        if names:
            p["authors"] = ", ".join(names)
        inv = w.get("abstract_inverted_index")
        if inv:
            words = sorted((i, word) for word, idxs in inv.items() for i in idxs)
            abstract = " ".join(word for _, word in words)
            abstract = re.sub(r"^\d+\s+", "", abstract)  # leading abstract number
            abstract = re.sub(r"^(abstract|background)\s*[:.]?\s*", "", abstract, flags=re.I)
            p["abstract"] = abstract
        if w.get("doi"):
            p["doi"] = w["doi"]
        oa = (w.get("best_oa_location") or {}).get("pdf_url")
        if oa:
            p["pdf"] = oa
        return True
    return False


MAX_AUTHORS = 1000  # show full author lists (lower this to shorten long lists with "…")


def is_me(a):
    a = a.strip().lower()
    if any(a == n.lower() for n in MY_NAMES):
        return True
    # e.g. "Xiaoyan A. Guo", "X.A. Guo", "Alex X. Guo"
    return a.endswith("guo") and bool(re.search(r"\b(xiaoyan|alex|xa)\b|^x\.", a))


def starred_for(title, table=None):
    for key, who in (EQUAL_CONTRIB if table is None else table).items():
        if key.lower() in title.lower():
            return who
    return []


def match_authors(names, mine, wanted):
    return {i for i, a in enumerate(names)
            for s in wanted
            if (s == "me" and i in mine) or (s != "me" and s.lower() in a.lower())}


def bold_me(authors, starred=(), daggered=()):
    names = [a.strip() for a in authors.split(", ") if a.strip()]
    mine = [i for i, a in enumerate(names) if is_me(a)]
    stars = match_authors(names, mine, starred)
    daggers = match_authors(names, mine, daggered)
    keep = set(range(len(names)))
    if len(names) > MAX_AUTHORS:
        keep = set(range(6)) | set(mine) | stars | daggers | {len(names) - 2, len(names) - 1}
    out, prev = [], -1
    for i, a in enumerate(names):
        if i not in keep:
            continue
        if i != prev + 1:
            out.append("&hellip;")
        a_esc = (html.escape(a) + ("*" if i in stars else "")
                 + ("&dagger;" if i in daggers else ""))
        out.append(f"<b>{a_esc}</b>" if i in mine else a_esc)
        prev = i
    return ", ".join(out)


def kind_of(p):
    for key, kind in FORCE_TYPE.items():
        if key.lower() in p["title"].lower():
            return kind
    t, v = p["title"], p["venue"]
    if (re.match(r"abstract\s+\d+", t, re.I)
            or re.search(r":\s*\d+\s*$", t)
            or re.search(r"\(\d+_(suppl|supplement)\)", v, re.I)
            or re.search(r"\bS\d+\s*-\s*S\d+\b", v)
            or re.search(r"meeting abstracts|abstract book", v, re.I)):
        return "abstract"
    return "paper"


def am_first(p):
    names = [a.strip() for a in p["authors"].split(", ")]
    if names and is_me(names[0]):
        return True
    return "me" in starred_for(p["title"])


def clean_title(title_html):
    t = re.sub(r"^\s*abstract\s+\d+\s*[:.]?\s*", "", title_html, flags=re.I)
    return re.sub(r":\s*\d+\s*$", "", t)


def render_item(p):
    links = []
    if p.get("doi"):
        links.append(f'<a href="{html.escape(p["doi"])}">doi</a>')
    if p.get("pdf"):
        links.append(f'<a href="{html.escape(p["pdf"])}">pdf</a>')
    if p.get("scholar_url"):
        links.append(f'<a href="{html.escape(p["scholar_url"])}">scholar</a>')
    cites = f' <span class="cites">cited by {p["cites"]}</span>' if p["cites"] else ""
    year = f', {p["year"]}' if p.get("year") and str(p["year"]) not in p["venue"] else ""
    item = [
        "<li>",
        f'  <span class="ptitle">{clean_title(p["title_html"])}</span><br>',
        f'  {bold_me(p["authors"], starred_for(p["title"]),
                     starred_for(p["title"], CO_CORRESPONDING))}<br>',
        f'  <i>{html.escape(p["venue"])}{year}</i>{cites}<br>',
        f'  <span class="plinks">[{"] [".join(links)}]</span>' if links else "",
    ]
    if p.get("abstract"):
        item.append(f'  <details><summary>abstract</summary><p>{html.escape(p["abstract"])}</p></details>')
    item.append("</li>")
    return [x for x in item if x]


def render(pubs):
    pubs = sorted(pubs, key=lambda p: -(p["year"] or 0))
    papers = [p for p in pubs if kind_of(p) == "paper"]
    abstracts = [p for p in pubs if kind_of(p) == "abstract" and am_first(p)]
    lines = ['<h3 class="sec">Papers &amp; preprints</h3>']
    year = None
    for p in papers:
        if p["year"] != year:
            if year is not None:
                lines.append("</ol>")
            year = p["year"]
            lines.append(f'<h4 class="yr">{year or "n.d."}</h4>')
            lines.append('<ol class="pubs">')
        lines.extend(render_item(p))
    if year is not None:
        lines.append("</ol>")
    if abstracts:
        lines.append('<h3 class="sec">Conference abstracts</h3>')
        lines.append('<ol class="pubs">')
        for p in abstracts:
            lines.extend(render_item(p))
        lines.append("</ol>")
    today = datetime.date.today().strftime("%b %-d, %Y")
    lines.append(f'<p class="small">auto-updated from Google Scholar on {today}.</p>')
    return "\n".join(lines)


def main():
    try:
        pubs = scrape_scholar()
    except Exception as e:
        print(f"Scholar fetch failed, keeping existing list: {e}", file=sys.stderr)
        return 0
    print(f"found {len(pubs)} publications on Scholar")
    cache = load_cache()
    for p in pubs:
        key = norm(p["title"])
        if openalex_enrich(p):
            cache[key] = {k: p[k] for k in ENRICHED if k in p}
        elif key in cache:  # lookup failed: reuse the last good result
            p.update(cache[key])
        time.sleep(1.5)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, ensure_ascii=False, sort_keys=True)
    with open(INDEX, encoding="utf-8") as f:
        page = f.read()
    new = re.sub(r"(<!-- PUBS:START -->).*?(<!-- PUBS:END -->)",
                 lambda m: f"{m.group(1)}\n{render(pubs)}\n{m.group(2)}", page, flags=re.S)
    with open(INDEX, "w", encoding="utf-8") as f:
        f.write(new)
    print("index.html updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
