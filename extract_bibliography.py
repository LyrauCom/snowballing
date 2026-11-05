#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Aurelie Jeunejean. See LICENSE.
"""
Extract the reference lists of a folder of PDF articles with GROBID, for the
backward snowballing stage of a systematic literature review (SLR).

Each PDF is sent to the GROBID processReferences service. The parsed
references are stored in bibliography.csv, which is also YOUR WORKING FILE:
the script never deletes what you typed in it (manual rows, overrides).

Folders (relative to the folder the script is launched from, both can be changed):
  ./PDF      the PDF articles (--pdf-dir)
  ./output   every file produced by the script (--output-dir)

Files written in the output folder (all CSV files are UTF-8 with BOM, so that
Excel opens them correctly):

  bibliography.csv                one row per extracted reference (working file)
  bibliography_unique.csv         one row per unique reference: screening candidates
  bibliography_checks.csv         cases to verify manually (your annotations are kept)
  bibliography_grey_validation.csv  10 % sample to validate the grey literature
                                  rules (only with --flag-grey, annotations kept)
  bibliography_manifest.csv       one row per PDF: fingerprint, status, error
  bibliography_README.txt         what each file is and what to do with it
  run_log.json                    history of the runs (GROBID version, settings,
                                  counts, failures): use it for the methods section
  backups/                        copy of bibliography.csv before each run (5 kept)

Usage:
  python3 extract_bibliography.py
  python3 extract_bibliography.py --pdf-dir /path/to/pdf --output-dir /path/to/output
  python3 extract_bibliography.py --included covidence_included.csv
  python3 extract_bibliography.py --included included.csv \\
      --screened excluded.csv irrelevant.csv --flag-grey

PDF naming convention (checked when --included is given):
  <Covidence # on 3 digits>_<year>_<first author surname>_<title>.pdf
  (surname only, no "et al"), e.g.
  001_2024_Omer_Development of a 3D Digital Model of End-of-Service-Life Buildings.pdf

MANUAL CORRECTIONS in bibliography.csv (rerun the script afterwards):
  - Wrong DOI: correct the "doi" column; the dedup_key follows automatically.
  - Split a wrong merge (two different works received the same DOI, e.g.
    Ding 2018a and Ding 2018b): write a unique value in "dedup_override" on
    one of the rows, e.g.  manual:ding2018b
  - Force a merge (same work, different titles): write the same value in
    "dedup_override" on every row concerned, e.g.  manual:akanbi2020
  - Rows typed by hand (missing PDF...): set "origin" to  manual ; they
    are never overwritten by the script.

POSTERS AND BIBLIOGRAPHIES NOT RECOGNISED BY GROBID:
  GROBID is trained on full articles: on posters or very short papers it may
  miss the bibliography. Copy the references into a text file placed next to
  the PDF, with the same name and the extension .refs.txt, e.g.
      PDF/2026_Hofmann_From Inventory to Reuse.pdf
      PDF/2026_Hofmann_From Inventory to Reuse.refs.txt
  One reference per line; or, if a reference spans several lines, separate
  the references with an empty line. Lines starting with # are ignored.
  At the next run the script sends this text to GROBID (processCitationList),
  which structures the references and completes them with Crossref
  (origin = citation_list). Editing the file triggers a new processing.

PDF WITHOUT TEXT LAYER (scanned, printed to PDF, fonts without Unicode):
  detected before GROBID (status no_text, not sent). Get the original PDF, or
  use the same .refs.txt file as for posters. Copying from such a PDF gives
  nothing, so let the system read the text of a screenshot of the
  bibliography: macOS, take a screenshot (Cmd+Shift+4), open it in Preview and
  select the text; Windows 11, Snipping Tool, then "Text actions" and
  "Copy all text". Check accents, years and DOIs before saving.

Requires: Python 3.8 or later and the packages of requirements.txt (pypdf).
By hand:  python3 -m venv .venv
          .venv/bin/python -m pip install -r requirements.txt   (Windows: .venv\\Scripts\\python.exe)
          .venv/bin/python extract_bibliography.py
"""

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:  # the launchers install it; explain what to do when run by hand
    sys.stderr.write(
        "!! The Python package 'pypdf' is missing.\n"
        "   By hand, in the folder of the script:\n"
        "     python3 -m venv .venv\n"
        "     .venv/bin/python -m pip install -r requirements.txt      (Windows: .venv\\Scripts\\python.exe)\n"
        "     .venv/bin/python extract_bibliography.py\n")
    sys.exit(1)

# pypdf reports minor PDF defects ("Ignoring wrong pointing object") that do not matter here
logging.getLogger("pypdf").setLevel(logging.ERROR)

SCRIPT_VERSION = "3.3.3 (2026-09-30)"
DEFAULT_GROBID_URL = "http://localhost:8070"
TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}
CSV_ENCODING = "utf-8-sig"  # BOM for Excel; reading also accepts files without BOM
BACKUPS_KEPT = 5

FIELDNAMES = ["source_file", "ref_id", "origin", "authors", "title", "journal", "year",
              "volume", "pages", "doi", "raw_citation", "dedup_override", "dedup_key"]
MANIFEST_FIELDNAMES = ["source_file", "sha256", "status", "n_refs", "processed_at", "pages",
                       "chars_per_page", "biblio_heading", "producer", "refs_txt_sha256", "message"]

# origins written by the script (replaced when a PDF is processed again); "manual" rows are never touched
GENERATED_ORIGINS = {"grobid", "citation_list"}
MIN_CHARS_PER_PAGE = 300   # below: no usable text layer (scanned, printed to PDF, fonts without Unicode)
REFS_TXT_SUFFIX = ".refs.txt"
BIBLIO_HEADING = re.compile(
    r"^\s*(?:\d+[.)]?\s*)?(?:r[ée]f[ée]rences?(?: bibliographiques)?|references|bibliograph\w*|"
    r"works cited|literature cited|literatur(?:verzeichnis)?|sources)\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE)
UNIQUE_FIELDNAMES = ["dedup_key", "title", "authors", "year", "journal", "doi", "cited_by_count",
                     "cited_by_files", "years_in_raw", "origin", "in_seed_corpus", "raw_citation"]
CHECK_FIELDNAMES = ["severity", "check", "source_file", "ref_id", "dedup_key", "doi",
                    "title", "year", "raw_citation", "details", "verified", "decision"]
GREY_VALIDATION_FIELDNAMES = ["dedup_key", "sample_group", "likely_grey", "grey_reason", "title",
                              "authors", "year", "journal", "doi", "raw_citation",
                              "reviewer_is_grey", "reviewer", "comment"]

# ---------------------------------------------------------------------------
# Quality check thresholds
# ---------------------------------------------------------------------------
MIN_TITLE_OVERLAP = 0.3         # share of Crossref title words found in the raw citation
MAX_RAW_CITATION_LENGTH = 500   # longer raw citations probably merge several references
MIN_FILENAME_TITLE_OVERLAP = 0.7
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# ---------------------------------------------------------------------------
# Grey literature rules (only used with --flag-grey).
# ADAPT THEM TO EACH SLR: they were written for a corpus on circular
# construction (EU institutions, standards, English and French citations).
# Rules are tried in order; the first match gives the reason.
# "strong" rules apply to every reference. "weak" rules are skipped when the
# reference has both a DOI and a journal (probably peer-reviewed).
# ---------------------------------------------------------------------------
GREY_DOI_PREFIXES = {
    "10.2785/": "EU Publications Office",
    "10.2760/": "EU Publications Office (JRC)",
    "10.1787/": "OECD",
    "10.59117/": "UNEP",
    "10.6027/": "Nordic Council of Ministers",
}
GREY_RULES = [
    # (reason, strength, regex, regex flags, known false positive)
    ("standard", "strong", r"\b(?:EN|ISO|IS|BS|NS|DIN|ASTM|IRC|AS/NZS)[\s:]*\d", 0,
     "an acronym followed by a number inside a title (case-sensitive to limit it)"),
    ("web page (accessed/online)", "weak", r"\baccessed\b|\bretrieved\b|\[online\]", re.IGNORECASE,
     "peer-reviewed article cited with an access date and no DOI"),
    ("thesis", "weak", r"\b(?:thesis|dissertation|master'?s?|phd)\b", re.IGNORECASE,
     "article whose title contains 'thesis' or 'master plan'"),
    ("report/guidance", "weak",
     r"\b(?:report|white paper|guidance|guidelines?|protocol|deliverable|toolkit|policy brief|working paper)\b",
     re.IGNORECASE, "article whose title contains 'guidelines' or 'protocol'"),
    ("institutional author", "weak",
     r"\b(?:european commission|eurostat|directorate-general|ministry|agency|foundation|world economic forum|unep|oecd)\b",
     re.IGNORECASE, "article published by an author affiliated with an agency"),
    ("project page", "weak", r"\bgrant agreement\b|\bhorizon 2020\b|\binterreg\b", re.IGNORECASE,
     "article that acknowledges a project in its citation"),
]
GREY_FALLBACK_REASON = "no DOI and no venue"  # known false positive: conference paper without DOI
GREY_SAMPLE_MODULO = 10  # 1 reference out of 10, chosen from its dedup_key (stable across runs)

YEAR_PATTERN = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$")
FILENAME_PATTERN = re.compile(r"^(\d{3})_(\d{4})_([^_]+)_(.+)\.pdf$", re.IGNORECASE)


def log(message=""):
    print(message, file=sys.stderr)


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def fold(text):
    """Lower-case ASCII transliteration: 'Rašković' -> 'raskovic'."""
    text = unicodedata.normalize("NFKD", text or "")
    return text.encode("ascii", "ignore").decode("ascii").lower()


def normalize_title(title):
    t = re.sub(r"[^a-z0-9 ]", " ", fold(title))
    return re.sub(r"\s+", " ", t).strip()


def significant_words(text):
    return [w for w in normalize_title(text).split() if len(w) > 3 and not w.isdigit()]


def word_overlap(reference_text, other_text):
    """Share of the significant words of reference_text found in other_text."""
    ref = significant_words(reference_text)
    other = set(significant_words(other_text))
    if not ref or not other:
        return None
    return sum(1 for w in ref if w in other) / len(ref)


def clean_doi(doi):
    """Remove URL prefixes, spaces and trailing punctuation; lower-case."""
    if not doi:
        return ""
    d = doi.strip()
    d = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", d, flags=re.IGNORECASE)
    d = re.sub(r"^doi:\s*", "", d, flags=re.IGNORECASE)
    d = re.sub(r"\s+", "", d)
    found = re.search(r"10\.\d{4,9}/\S+", d)  # drops leftovers such as ".org/" before the DOI
    if found:
        d = found.group(0)
    d = d.rstrip(".,;)")
    return d.lower()


def first_author_surname(authors):
    if not authors:
        return ""
    first = authors.split(";")[0].strip()
    return normalize_title(first.split(" ")[-1]) if first else ""


def raw_citation_year(record):
    """Year as written in the raw citation (the structured year is preferred if present in it)."""
    years = YEAR_PATTERN.findall(record.get("raw_citation") or "")
    if not years:
        return None
    return record["year"] if record.get("year") in years else years[0]


# ---------------------------------------------------------------------------
# CSV helpers: atomic writes, BOM, backups
# ---------------------------------------------------------------------------

def read_csv(path, expected=None, own=False):
    """Read a CSV written by the script or re-saved by Excel, Numbers or LibreOffice.

    Accepts UTF-8 (with or without BOM) or the legacy encodings of Excel, and a
    comma, semicolon or tab separator (French Excel saves with semicolons).
    'own' marks the files written by the script, for the note about re-saved files.
    'expected' lists columns that must be present: without them, the script stops
    instead of silently losing what was typed in the file.
    """
    if not path.exists():
        return []
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252" if os.name == "nt" else "mac_roman"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    first_line = text.split("\n", 1)[0]
    delimiter = max([",", ";", "\t"], key=first_line.count)
    rows = list(csv.DictReader(text.splitlines(keepends=True), delimiter=delimiter))
    rows = [{(k or "").strip(): v for k, v in r.items()} for r in rows]
    if expected and rows:
        missing = [c for c in expected if c not in rows[0]]
        if missing:
            log(f"!! {path.name}: column(s) {', '.join(missing)} not found (separator read: {delimiter!r}).")
            log("   The file was probably re-saved in another format. Save it again as")
            log("   'CSV UTF-8 (comma delimited)', or restore it from the backups folder, then rerun.")
            sys.exit(1)
    if own and (delimiter != "," or not raw.startswith(b"\xef\xbb\xbf")):
        log(f"   note: {path.name} was re-saved by another program (separator {delimiter!r}); "
            "it is read correctly and will be rewritten in the standard format.")
    return rows


def write_csv_atomic(path, fieldnames, rows):
    """Write to a temporary file, then replace the target in one step."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=CSV_ENCODING, newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in fieldnames})
        os.replace(tmp, path)
    except PermissionError:
        if os.path.exists(tmp):
            os.remove(tmp)
        log(f"!! Cannot write {path}: the file is probably open in Excel or another program.")
        log("   Close it and run the script again (everything saved before this point is kept).")
        sys.exit(1)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def backup(path):
    if not path.exists():
        return None
    folder = path.parent / "backups"
    folder.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    target = folder / f"{path.stem}_{stamp}{path.suffix}"
    shutil.copy2(path, target)
    old = sorted(folder.glob(f"{path.stem}_*{path.suffix}"))
    for extra in old[:-BACKUPS_KEPT]:
        extra.unlink()
    return target


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# GROBID
# ---------------------------------------------------------------------------

EMPTY_RESULT_HINT = "no reference found and no bibliography heading detected in the text"
NOT_RECOGNISED_HINT = ("a bibliography is present but GROBID did not recognise it (common for posters and short "
                       "papers): copy the references into '{name}' next to the PDF and rerun")
NO_TEXT_HINT = ("no usable text layer ({cpp} characters per page): scanned or printed to PDF, or fonts without "
                "Unicode mapping. Use the original PDF, or copy the references into '{name}' (see README: screenshot text recognition)")


class GrobidUnavailable(Exception):
    """The GROBID server does not answer."""


def http_get(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def grobid_version(grobid_url):
    """Return the GROBID version, or raise GrobidUnavailable if the server does not answer."""
    base = grobid_url.rstrip("/")
    try:
        http_get(f"{base}/api/isalive", timeout=10)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise GrobidUnavailable(str(e)) from e
    try:
        text = http_get(f"{base}/api/version", timeout=10).strip()
    except (urllib.error.URLError, OSError):
        return "unknown"
    try:
        data = json.loads(text)
        return str(data.get("version", text)) if isinstance(data, dict) else text
    except ValueError:
        return text


def multipart_body(fields, file_field=None, file_path=None):
    """Encode form fields (list of (name, value), names may repeat) and an optional file as multipart/form-data."""
    boundary = uuid.uuid4().hex
    chunks = []
    for name, value in fields:
        chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n"
                       f"Content-Type: text/plain; charset=UTF-8\r\n\r\n{value}\r\n").encode("utf-8"))
    if file_path is not None:
        filename = file_path.name.replace('"', "'")
        chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                       f"filename=\"{filename}\"\r\nContent-Type: application/pdf\r\n\r\n").encode("utf-8"))
        chunks.append(file_path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def post_grobid(url, body, content_type, timeout):
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace").strip()
        title = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
        text = (title.group(1).strip() if title else " ".join(text.split()))[:300]
        hint = ""
        if "NO_BLOCKS" in text:
            hint = " -> no text layer: printed or scanned PDF, get the original PDF or use a .refs.txt file"
        raise RuntimeError(f"HTTP {e.code}: {text}{hint}") from None


def call_grobid(pdf_path, grobid_url, timeout, consolidate):
    """processReferences: find and parse the bibliography of a PDF."""
    body, content_type = multipart_body([("consolidateCitations", consolidate), ("includeRawCitations", "1")],
                                        "input", pdf_path)
    return post_grobid(f"{grobid_url.rstrip('/')}/api/processReferences", body, content_type, timeout)


def call_grobid_citations(citations, grobid_url, timeout, consolidate):
    """processCitationList: parse references given as text (posters, bibliographies GROBID missed)."""
    fields = [("consolidateCitations", consolidate), ("includeRawCitations", "1")]
    fields += [("citations", c) for c in citations]
    # This endpoint only accepts a url-encoded form (HTTP 415 with multipart/form-data)
    body = urllib.parse.urlencode(fields).encode("utf-8")
    return post_grobid(f"{grobid_url.rstrip('/')}/api/processCitationList", body,
                       "application/x-www-form-urlencoded; charset=UTF-8", timeout)


def text_or_none(el):
    if el is None:
        return None
    txt = "".join(el.itertext()).strip()
    return txt or None


def parse_biblstruct(bibl_struct, source_file):
    ns = TEI_NS
    title = bibl_struct.find(".//tei:analytic/tei:title", ns)
    if title is None:
        title = bibl_struct.find(".//tei:monogr/tei:title", ns)
    title_text = text_or_none(title)

    authors = []
    container = bibl_struct.find(".//tei:analytic", ns)
    if container is None or container.find("tei:author", ns) is None:
        container = bibl_struct.find(".//tei:monogr", ns)
    if container is not None:
        for author in container.findall("tei:author", ns):
            pers = author.find("tei:persName", ns)
            if pers is None:
                continue
            forenames = [text_or_none(fn) for fn in pers.findall("tei:forename", ns)]
            surname = text_or_none(pers.find("tei:surname", ns))
            name = " ".join([f for f in forenames if f] + ([surname] if surname else []))
            if name:
                authors.append(name)

    journal = bibl_struct.find(".//tei:monogr/tei:title", ns)
    journal_text = text_or_none(journal) if journal is not title else None

    date_el = bibl_struct.find(".//tei:monogr//tei:date", ns)
    year = date_el.attrib.get("when") if date_el is not None else None
    year = year[:4] if year and len(year) >= 4 else year

    volume = text_or_none(bibl_struct.find(".//tei:monogr//tei:biblScope[@unit='volume']", ns))
    pages_el = bibl_struct.find(".//tei:monogr//tei:biblScope[@unit='page']", ns)
    pages = None
    if pages_el is not None:
        pages = pages_el.attrib.get("from", "")
        if pages_el.attrib.get("to"):
            pages = f"{pages}-{pages_el.attrib['to']}"
        pages = pages or text_or_none(pages_el)

    doi = None
    for idno in bibl_struct.findall(".//tei:idno", ns):
        if idno.attrib.get("type", "").lower() == "doi":
            doi = text_or_none(idno)

    return {
        "source_file": source_file,
        "ref_id": bibl_struct.attrib.get("{http://www.w3.org/XML/1998/namespace}id"),
        "origin": "grobid",
        "authors": "; ".join(authors) if authors else None,
        "title": title_text,
        "journal": journal_text,
        "year": year,
        "volume": volume,
        "pages": pages,
        "doi": doi,
        "raw_citation": text_or_none(bibl_struct.find(".//tei:note[@type='raw_reference']", ns)),
        "dedup_override": "",
    }


def parse_tei_references(tei_xml, source_file, origin="grobid", id_prefix=None):
    """Every biblStruct of a GROBID answer (a TEI document, a listBibl or bare biblStruct elements)."""
    try:
        root = ET.fromstring(tei_xml)
    except ET.ParseError:
        root = ET.fromstring(f'<wrapper xmlns="{TEI_NS["tei"]}">{tei_xml}</wrapper>')
    structs = [root] if root.tag == f"{{{TEI_NS['tei']}}}biblStruct" else root.findall(".//tei:biblStruct", TEI_NS)
    records = []
    for i, b in enumerate(structs):
        record = parse_biblstruct(b, source_file)
        record["origin"] = origin
        if id_prefix:
            record["ref_id"] = f"{id_prefix}{i}"
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# PDF diagnosis and .refs.txt files
# ---------------------------------------------------------------------------

def diagnose_pdf(pdf_path):
    """Pages, characters per page, bibliography heading and producer, read with pypdf."""
    info = {"pages": 0, "chars_per_page": 0, "biblio_heading": "no", "producer": "", "error": ""}
    try:
        reader = PdfReader(str(pdf_path))
        if reader.is_encrypted:
            reader.decrypt("")
        info["pages"] = len(reader.pages)
        texts = []
        for page in reader.pages:
            try:
                texts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - a broken page must not stop the diagnosis
                texts.append("")
        text = "\n".join(texts)
        info["chars_per_page"] = len(text.strip()) // max(info["pages"], 1)
        info["biblio_heading"] = "yes" if BIBLIO_HEADING.search(text) else "no"
        producer = (reader.metadata or {}).get("/Producer", "")
        if hasattr(producer, "get_object"):
            producer = producer.get_object()
        info["producer"] = str(producer or "")[:80]
    except Exception as e:  # noqa: BLE001 - unreadable PDF: let GROBID try anyway
        info["error"] = str(e)[:200]
    return info


def refs_txt_path(pdf_path):
    return pdf_path.with_name(pdf_path.stem + REFS_TXT_SUFFIX)


def read_citations(path):
    """References of a .refs.txt file: one per line, or blocks separated by an empty line."""
    lines = [l.rstrip() for l in path.read_text(encoding="utf-8-sig").splitlines() if not l.lstrip().startswith("#")]
    if any(not l.strip() for l in lines) and any(l.strip() for l in lines):
        blocks, current = [], []
        for l in lines:
            if l.strip():
                current.append(l.strip())
            elif current:
                blocks.append(" ".join(current))
                current = []
        if current:
            blocks.append(" ".join(current))
        return blocks
    return [l.strip() for l in lines if l.strip()]


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def compute_dedup_key(record):
    """dedup_override if filled, else DOI, else normalised title + first author."""
    override = (record.get("dedup_override") or "").strip()
    if override:
        return override
    doi = clean_doi(record.get("doi"))
    if doi:
        return "doi:" + doi
    norm_title = normalize_title(record.get("title"))
    if not norm_title:
        return ""
    return f"title:{norm_title}|{first_author_surname(record.get('authors'))}"


def representative(refs):
    """The row whose title best matches its own raw citation; every field comes from it."""
    def score(r):
        overlap = word_overlap(r.get("title"), r.get("raw_citation"))
        return (overlap if overlap is not None else 0.5, bool(r.get("doi")), len(r.get("title") or ""))
    return max(refs, key=score)


# ---------------------------------------------------------------------------
# Covidence exports: seed corpus and PDF names
# ---------------------------------------------------------------------------

def load_covidence(paths):
    records = []
    for path in paths or []:
        for row in read_csv(Path(path)):
            number = (row.get("Covidence #") or "").strip().lstrip("#")
            records.append({
                "id": int(number) if number.isdigit() else None,
                "label": row.get("Covidence #", ""),
                "title": row.get("Title", ""),
                "authors": row.get("Authors", ""),
                "year": (row.get("Published Year") or "").strip(),
                "doi": clean_doi(row.get("DOI")),
                "study": row.get("Study", ""),
                "file": Path(path).name,
            })
    return records


def build_seed_index(seed_records):
    by_doi = {r["doi"]: r for r in seed_records if r["doi"]}
    by_title = {normalize_title(r["title"]): r for r in seed_records if normalize_title(r["title"])}
    return by_doi, by_title


def find_in_seed(ref, index):
    by_doi, by_title = index
    doi = clean_doi(ref.get("doi"))
    if doi and doi in by_doi:
        return by_doi[doi]
    return by_title.get(normalize_title(ref.get("title")))


def covidence_surname(record):
    """First author's surname as written in Covidence ('Omer, M; Wang, YC' -> 'Omer')."""
    return record["authors"].split(";")[0].split(",")[0].strip()


def suggested_pdf_name(name, record):
    """Name following the convention, keeping the title part of the current name."""
    parts = [part.strip() for part in Path(name).stem.split("_") if part.strip()]
    title = parts[-1] if len(parts) > 1 else Path(name).stem
    return f"{record['id']:03d}_{record['year']}_{covidence_surname(record)}_{title}.pdf"


def check_pdf_names(pdf_names, included):
    """Compare PDF names with the Covidence export of included studies."""
    checks = []
    by_id = {r["id"]: r for r in included if r["id"] is not None}
    seen_ids = defaultdict(list)
    for name in pdf_names:
        match = FILENAME_PATTERN.match(name)
        prefix = name[:3]
        if not match:
            record_for_hint = by_id.get(int(prefix)) if prefix.isdigit() else None
            hint = f" Suggested name: {suggested_pdf_name(name, record_for_hint)}" if record_for_hint else ""
            checks.append(check_row("medium", "PDF_NAME_PATTERN", {"source_file": name},
                                    "Name does not follow <###>_<year>_<first author surname>_<title>.pdf." + hint))
        if prefix.isdigit():
            seen_ids[int(prefix)].append(name)
        if match and (match.group(3) != match.group(3).strip() or match.group(4) != match.group(4).strip()):
            checks.append(check_row("low", "PDF_NAME_SPACES", {"source_file": name},
                                    "Leading/trailing space around an underscore in the name"))
        record = by_id.get(int(prefix)) if prefix.isdigit() else None
        if record is None:
            checks.append(check_row("high", "PDF_NOT_IN_INCLUDED", {"source_file": name},
                                    "No included study with this Covidence # in the export: duplicate, excluded study or wrong number?"))
            continue
        if not match:
            continue
        _, year, author, title = match.groups()
        suggestion = suggested_pdf_name(name, record)
        if year != record["year"]:
            checks.append(check_row("medium", "PDF_NAME_YEAR", {"source_file": name},
                                    f"Year {year} in the name, {record['year']} in Covidence ({record['label']}) (online vs print year?). Suggested name: {suggestion}"))
        surname = covidence_surname(record)
        if surname and normalize_title(author) != normalize_title(surname):
            reason = ("'et al' is not part of the convention (first author's surname only)"
                      if re.search(r"\bet\s+al\b", author, re.IGNORECASE) and normalize_title(surname) in normalize_title(author)
                      else f"first author '{author.strip()}' in the name, '{surname}' in Covidence")
            checks.append(check_row("medium", "PDF_NAME_AUTHOR", {"source_file": name},
                                    f"{reason} ({record['label']}). Suggested name: {suggestion}"))
        overlap = word_overlap(record["title"], title)
        if overlap is not None and overlap < MIN_FILENAME_TITLE_OVERLAP:
            checks.append(check_row("high", "PDF_NAME_TITLE", {"source_file": name},
                                    f"Only {overlap:.0%} of the Covidence title words in the name ({record['label']}: {record['title'][:80]})"))
    for number, names in seen_ids.items():
        if len(names) > 1:
            for name in names:
                checks.append(check_row("high", "PDF_SAME_NUMBER", {"source_file": name},
                                        f"{len(names)} PDFs share the number {number:03d}"))
    for record in included:
        if record["id"] is not None and record["id"] not in seen_ids:
            checks.append(check_row("medium", "INCLUDED_WITHOUT_PDF", {"source_file": f"{record['id']:03d} (no PDF)", "title": record["title"]},
                                    f"Included study {record['label']} ({record['study']}) has no PDF: reference list not explored"))
    # two PDFs describing the same article under different numbers
    titles = [(n, FILENAME_PATTERN.match(n).group(4)) for n in pdf_names if FILENAME_PATTERN.match(n)]
    for i, (name_a, title_a) in enumerate(titles):
        for name_b, title_b in titles[i + 1:]:
            ab, ba = word_overlap(title_a, title_b), word_overlap(title_b, title_a)
            if ab is not None and ba is not None and min(ab, ba) >= 0.9:
                for name, other in ((name_a, name_b), (name_b, name_a)):
                    checks.append(check_row("high", "PDF_POSSIBLE_DUPLICATE", {"source_file": name},
                                            f"Same title as {other}: its references would be counted twice"))
    return checks


# ---------------------------------------------------------------------------
# Grey literature
# ---------------------------------------------------------------------------

def classify_grey(rep, raws):
    doi = clean_doi(rep.get("doi"))
    for prefix, publisher in GREY_DOI_PREFIXES.items():
        if doi.startswith(prefix):
            return True, f"institutional DOI ({publisher})"
    text = " ".join(raws + [rep.get("title") or ""])
    peer_reviewed_hint = bool(doi and rep.get("journal"))
    for reason, strength, pattern, flags, _ in GREY_RULES:
        if strength == "weak" and peer_reviewed_hint:
            continue
        if re.search(pattern, text, flags):
            return True, reason
    if not doi and not rep.get("journal"):
        return True, GREY_FALLBACK_REASON
    return False, ""


def in_grey_sample(dedup_key):
    digest = hashlib.sha1(dedup_key.encode("utf-8")).hexdigest()
    return int(digest, 16) % GREY_SAMPLE_MODULO == 0


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def build_unique(records, seed_index, flag_grey):
    groups = defaultdict(list)
    for r in records:
        if r.get("dedup_key"):
            groups[r["dedup_key"]].append(r)
    rows = []
    for key, refs in groups.items():
        rep = representative(refs)
        years = sorted({y for y in (raw_citation_year(r) for r in refs) if y})
        seed = find_in_seed(rep, seed_index) if seed_index else None
        row = {
            "dedup_key": key,
            "title": rep.get("title"),
            "authors": rep.get("authors"),
            "year": rep.get("year"),
            "journal": rep.get("journal"),
            "doi": rep.get("doi"),
            "cited_by_count": len({r["source_file"] for r in refs}),
            "cited_by_files": "; ".join(sorted({r["source_file"] for r in refs})),
            "years_in_raw": "; ".join(years),
            "origin": "; ".join(sorted({r.get("origin") or "grobid" for r in refs})),
            "in_seed_corpus": f"{seed['label']} ({seed['file']})" if seed else "",
            "raw_citation": rep.get("raw_citation"),
        }
        if flag_grey:
            grey, reason = classify_grey(rep, [r.get("raw_citation") or "" for r in refs])
            row["likely_grey"] = "yes" if grey else "no"
            row["grey_reason"] = reason
        rows.append(row)
    rows.sort(key=lambda r: (-r["cited_by_count"], r["dedup_key"]))
    return rows


def check_row(severity, check, record, details):
    return {
        "severity": severity, "check": check,
        "source_file": record.get("source_file"), "ref_id": record.get("ref_id"),
        "dedup_key": record.get("dedup_key"), "doi": record.get("doi"),
        "title": record.get("title"), "year": record.get("year"),
        "raw_citation": record.get("raw_citation"), "details": details,
        "verified": "", "decision": "",
    }


def run_quality_checks(records, manifest, min_refs):
    checks = []
    by_source = defaultdict(list)
    for r in records:
        by_source[r["source_file"]].append(r)

    for m in manifest.values():
        if m["status"] == "failed":
            checks.append(check_row("high", "PDF_FAILED", {"source_file": m["source_file"]}, m["message"]))
        elif m["status"] == "no_text":
            checks.append(check_row("high", "PDF_NO_TEXT", {"source_file": m["source_file"]}, m["message"]))
        elif m["status"] == "no_refs" and m.get("biblio_heading") == "yes" and not m.get("refs_txt_sha256"):
            checks.append(check_row("high", "BIBLIOGRAPHY_NOT_RECOGNISED", {"source_file": m["source_file"]}, m["message"]))
        elif m["status"] == "no_refs":
            checks.append(check_row("high", "PDF_NO_REFERENCE", {"source_file": m["source_file"]},
                                    "GROBID found no reference (" + EMPTY_RESULT_HINT + "). Then rerun with --retry-empty, or type the references by hand"))
        elif m["status"] == "missing":
            checks.append(check_row("medium", "PDF_MISSING", {"source_file": m["source_file"]},
                                    "Rows in bibliography.csv but the PDF is no longer in the folder: rerun with --prune-missing to remove them"))
        elif m["status"] == "changed":
            checks.append(check_row("medium", "PDF_CHANGED", {"source_file": m["source_file"]},
                                    "The PDF changed since its extraction: rerun with --reprocess-changed"))

    for source, refs in by_source.items():
        if len(refs) < min_refs:
            checks.append(check_row("medium", "FEW_REFERENCES", {"source_file": source},
                                    f"Only {len(refs)} reference(s): compare with the printed bibliography"))
        same_doi = defaultdict(list)
        for r in refs:
            if clean_doi(r.get("doi")) and not (r.get("dedup_override") or "").strip():
                same_doi[clean_doi(r["doi"])].append(r)
        for doi, same in same_doi.items():
            if len(same) > 1:
                ids = ", ".join(r.get("ref_id") or "?" for r in same)
                for r in same:
                    checks.append(check_row("high", "DOI_DUPLICATE_IN_ARTICLE", r,
                                            f"{len(same)} references of this article share this DOI ({ids}): split them with dedup_override"))

    for r in records:
        doi = clean_doi(r.get("doi"))
        if doi and not DOI_PATTERN.match(doi):
            checks.append(check_row("medium", "DOI_INVALID", r, f"'{r.get('doi')}' does not look like a DOI"))
        if doi and (r.get("origin") or "grobid") in GENERATED_ORIGINS:
            overlap = word_overlap(r.get("title"), r.get("raw_citation"))
            if overlap is not None and overlap < MIN_TITLE_OVERLAP:
                checks.append(check_row("high", "DOI_TITLE_MISMATCH", r,
                                        f"Only {overlap:.0%} of the Crossref title words appear in the raw citation"))
        if len(r.get("raw_citation") or "") > MAX_RAW_CITATION_LENGTH:
            checks.append(check_row("medium", "LONG_RAW_CITATION", r,
                                    f"Raw citation of {len(r['raw_citation'])} characters: possibly several merged references"))
        if not r.get("dedup_key"):
            checks.append(check_row("low", "NO_DEDUP_KEY", r, "No DOI and no title: absent from bibliography_unique.csv"))

    doi_groups = defaultdict(list)
    for r in records:
        if (r.get("dedup_key") or "").startswith("doi:"):
            doi_groups[r["dedup_key"]].append(r)
    for refs in doi_groups.values():
        if len({r["source_file"] for r in refs}) < 2:
            continue
        years = {int(y) for y in (raw_citation_year(r) for r in refs) if y}
        if years and max(years) - min(years) > 1:
            spread = ", ".join(str(y) for y in sorted(years))
            for r in refs:
                checks.append(check_row("medium", "DOI_GROUP_YEAR_SPREAD", r,
                                        f"Citations of this DOI give different years ({spread}): different works merged?"))
    return checks


def merge_annotations(new_rows, old_rows, key_fields, annotation_fields):
    """Copy the annotations typed in the previous version of a file; return the orphans count."""
    old = {tuple(unicodedata.normalize("NFC", r.get(k, "") or "").strip() for k in key_fields): r for r in old_rows}
    used = set()
    for row in new_rows:
        key = tuple(unicodedata.normalize("NFC", str(row.get(k, "") or "")).strip() for k in key_fields)
        if key in old:
            used.add(key)
            for field in annotation_fields:
                row[field] = old[key].get(field, "") or row.get(field, "")
    return sum(1 for key, r in old.items()
               if key not in used and any((r.get(f) or "").strip() for f in annotation_fields))


def write_checks(path, checks):
    old = read_csv(path, expected=["check", "source_file", "verified", "decision"], own=True)
    if any((r.get("verified") or r.get("decision") or "").strip() for r in old):
        backup(path)  # annotations typed by hand are never lost
    orphans = merge_annotations(checks, old, ["check", "source_file", "ref_id", "doi"], ["verified", "decision"])
    checks.sort(key=lambda c: (SEVERITY_ORDER[c["severity"]], c["check"], c["dedup_key"] or "", c["source_file"] or ""))
    write_csv_atomic(path, CHECK_FIELDNAMES, checks)
    return orphans


def write_grey_validation(path, unique_rows):
    rows = []
    for u in unique_rows:
        if in_grey_sample(u["dedup_key"]):
            rows.append({
                "dedup_key": u["dedup_key"],
                "sample_group": "flagged" if u["likely_grey"] == "yes" else "not_flagged",
                "likely_grey": u["likely_grey"], "grey_reason": u["grey_reason"],
                "title": u["title"], "authors": u["authors"], "year": u["year"],
                "journal": u["journal"], "doi": u["doi"], "raw_citation": u["raw_citation"],
                "reviewer_is_grey": "", "reviewer": "", "comment": "",
            })
    orphans = merge_annotations(rows, read_csv(path, expected=["dedup_key"], own=True), ["dedup_key"], ["reviewer_is_grey", "reviewer", "comment"])
    rows.sort(key=lambda r: (r["sample_group"], r["dedup_key"]))
    write_csv_atomic(path, GREY_VALIDATION_FIELDNAMES, rows)

    stats = {"sample_size": len(rows), "orphan_annotations": orphans}
    for group in ("flagged", "not_flagged"):
        done = [r for r in rows if r["sample_group"] == group and r["reviewer_is_grey"].strip().lower() in ("yes", "no")]
        grey = sum(1 for r in done if r["reviewer_is_grey"].strip().lower() == "yes")
        stats[group] = {"in_sample": sum(1 for r in rows if r["sample_group"] == group),
                        "reviewed": len(done), "confirmed_grey": grey}
    return stats


README_TEXT = """\
bibliography_README.txt, written by extract_bibliography.py {version} on {date}

FILES
  bibliography.csv
      One row per extracted reference. This is your WORKING FILE: edit it, the
      script keeps your changes. Columns to use:
        origin          where the reference comes from:
                          grobid         extracted from the PDF
                          citation_list  parsed from a <name>.refs.txt file (posters...)
                          manual         typed by hand, never modified by the script
        doi             correct a wrong DOI here
        dedup_override  force the grouping of a reference:
                          split a wrong merge -> unique value, e.g. manual:ding2018b
                          force a merge       -> same value on every row, e.g. manual:akanbi2020
      Rerun the script after any edit: every other file is recomputed.

  bibliography_unique.csv
      One row per unique reference = screening candidates, sorted by
      cited_by_count (use it to ORDER the screening, never as a threshold).
        years_in_raw    years found in the raw citations: several years far
                        apart = probably different works merged under one DOI
        in_seed_corpus  already in the Covidence exports given with --included /
                        --screened: do not screen it again (PRISMA: "already identified")
        likely_grey     (with --flag-grey) pre-screening, NOT a decision

  bibliography_checks.csv
      Cases to verify, sorted by severity. Fill "verified" (yes) and
      "decision" (e.g. "doi corrected", "override manual:x", "ok as is").
      Your annotations are kept at the next run. Fix the problem in
      bibliography.csv (or rename the PDF), then rerun.

  bibliography_grey_validation.csv   (only with --flag-grey)
      Random but stable 10 % sample of the unique references, flagged or not,
      to measure the error rate of the grey literature rules.
      INSTRUCTIONS:
        1. For each row, read title, journal and raw_citation (open the DOI if needed).
        2. Fill reviewer_is_grey with yes (grey literature according to the
           protocol: report, web page, standard, thesis, project document...)
           or no (peer-reviewed journal article, conference paper or book).
        3. Fill reviewer with your initials; comment if in doubt.
        4. Rerun the script: it prints the estimated error rates and stores them
           in run_log.json.
      Reading the result:
        flagged rows answered "no"      = false positives (relevant papers wrongly excluded)
        not_flagged rows answered "yes" = false negatives (grey literature left in)
      If false positives are not close to zero, verify every flagged reference
      by hand before excluding it.

  bibliography_manifest.csv
      One row per PDF: fingerprint (sha256), status, number of references,
      diagnosis (pages, characters per page, bibliography heading, producer)
      and message. Statuses: ok, no_refs (no reference found), no_text (no
      usable text layer, not sent to GROBID), failed, manual, missing, changed.

POSTERS AND BIBLIOGRAPHIES NOT RECOGNISED BY GROBID
  GROBID is trained on full articles and may miss the bibliography of a
  poster or of a very short paper (check BIBLIOGRAPHY_NOT_RECOGNISED).
  1. Create a text file next to the PDF, with the same name and the
     extension .refs.txt, e.g. "2026_Hofmann_Title.refs.txt".
  2. Copy the references from the PDF into it: one reference per line, or,
     if a reference spans several lines, separate them with an empty line.
     Lines starting with # are ignored.
  3. Rerun the script: GROBID structures these references and completes
     them with Crossref (origin = citation_list). Editing the file later
     triggers a new processing of the PDF.

PDF WITHOUT TEXT LAYER (check PDF_NO_TEXT)
  Scanned, printed to PDF, or fonts without Unicode mapping: the text looks
  fine on screen but cannot be read by a program, so the PDF is not sent to
  GROBID. In order of preference:
  1. get the original PDF (publisher, repository, LaTeX output) and replace it;
  2. create a .refs.txt file as above. Copying from this PDF gives nothing:
     take a screenshot of the bibliography and let the system read it
       macOS (12 or later): Cmd+Shift+4, open the image in Preview, select
                            the text, copy;
       Windows 11:          Snipping Tool, "Text actions", "Copy all text";
     then check accents, years and DOIs before saving the file.

  run_log.json
      History of the runs: GROBID version and settings, counts, failures.

  backups/
      Copy of bibliography.csv before each run (the {kept} most recent).

CURRENT GREY LITERATURE RULES (first match wins)
{rules}
"""


def rules_description():
    lines = [f"  institutional DOI prefix: " + ", ".join(f"{p} ({n})" for p, n in GREY_DOI_PREFIXES.items())]
    for reason, strength, pattern, _, false_positive in GREY_RULES:
        lines.append(f"  [{strength}] {reason}: /{pattern}/  (known false positive: {false_positive})")
    lines.append(f"  fallback: {GREY_FALLBACK_REASON} (known false positive: conference paper without DOI)")
    lines.append("  weak rules are skipped when the reference has both a DOI and a journal.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf-dir", type=Path, default=Path("PDF"), help="Folder containing the PDF files (default: ./PDF)")
    parser.add_argument("--output-dir", type=Path, default=Path("output"),
                        help="Folder for every file produced, backups included (default: ./output)")
    parser.add_argument("--grobid-url", default=DEFAULT_GROBID_URL, help=f"GROBID server (default: {DEFAULT_GROBID_URL})")
    # GROBID also accepts 2 (DOI only), deliberately not offered: it keeps the PDF title,
    # so a wrong DOI no longer triggers DOI_TITLE_MISMATCH and goes unnoticed
    parser.add_argument("--consolidate", choices=["0", "1"], default="1",
                        help="Completion of each reference with Crossref: 1 full (default), 0 none "
                             "(faster, for grey or non-English corpora). GROBID itself is still required")
    parser.add_argument("--timeout", type=int, default=300, help="Read timeout per PDF in seconds (default: 300)")
    parser.add_argument("--sleep", type=float, default=0.0, help="Pause between PDFs in seconds (default: 0)")
    parser.add_argument("--min-refs", type=int, default=10, help="Flag articles with fewer references (default: 10)")
    parser.add_argument("--included", type=Path, nargs="+", default=[],
                        help="Covidence export(s) of the INCLUDED studies: checks the PDF names and marks references already in the corpus")
    parser.add_argument("--screened", type=Path, nargs="+", default=[],
                        help="Other Covidence export(s) (excluded, irrelevant...): marks references already screened")
    parser.add_argument("--flag-grey", action="store_true", help="Flag likely grey literature and write the 10%% validation sample")
    parser.add_argument("--reprocess-changed", action="store_true", help="Re-extract PDFs whose content changed (manual rows are kept)")
    parser.add_argument("--prune-missing", action="store_true", help="Remove the rows of PDFs no longer in the folder")
    parser.add_argument("--retry-empty", action="store_true", help="Send again the PDFs for which GROBID found no reference")
    parser.add_argument("--dump-raw", type=Path, default=None, help="Debug: folder where the TEI XML of each PDF is saved")
    args = parser.parse_args()

    started = dt.datetime.now()
    if not args.pdf_dir.is_dir():
        log(f"!! PDF folder not found: {args.pdf_dir.resolve()}")
        log("   Create it, launch the script from the folder that contains it, or pass --pdf-dir.")
        sys.exit(1)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.output_dir / "bibliography.csv"
    stem = out_path.stem
    paths = {
        "unique": out_path.with_name(f"{stem}_unique.csv"),
        "checks": out_path.with_name(f"{stem}_checks.csv"),
        "grey": out_path.with_name(f"{stem}_grey_validation.csv"),
        "manifest": out_path.with_name(f"{stem}_manifest.csv"),
        "readme": out_path.with_name(f"{stem}_README.txt"),
        "log": out_path.with_name("run_log.json"),
    }

    log(f"PDF folder:    {args.pdf_dir.resolve()}")
    log(f"Output folder: {args.output_dir.resolve()}")

    # migration from versions <= 3.0, which wrote everything in the PDF folder
    migrated = []
    if not out_path.exists() and (args.pdf_dir / "bibliography.csv").exists():
        for name in ("bibliography.csv", "bibliography_manifest.csv", "bibliography_checks.csv",
                     "bibliography_grey_validation.csv", "run_log.json"):
            old = args.pdf_dir / name
            if old.exists():
                shutil.copy2(old, args.output_dir / name)
                migrated.append(name)
        if (args.pdf_dir / "backups").is_dir() and not (args.output_dir / "backups").exists():
            shutil.copytree(args.pdf_dir / "backups", args.output_dir / "backups")
            migrated.append("backups/")
        log(f"Files of a previous version copied from the PDF folder: {', '.join(migrated)}")
        log("   (the originals are left in the PDF folder: delete them once the new output folder is checked)")

    pdf_files = sorted(p for p in args.pdf_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    hashes = {p.name: sha256_of(p) for p in pdf_files}
    by_name = {p.name: p for p in pdf_files}

    # ---- load the working file (and migrate older versions) ----
    backup_path = backup(out_path)
    records = read_csv(out_path, expected=["source_file", "ref_id"], own=True)
    for r in records:
        r["origin"] = (r.get("origin") or "grobid").strip()
        r["dedup_override"] = (r.get("dedup_override") or "").strip()

    # ---- manifest: bootstrap from the CSV on the first run ----
    manifest = {m["source_file"]: m for m in read_csv(paths["manifest"], expected=["source_file", "sha256"], own=True)}
    sources = defaultdict(list)
    for r in records:
        sources[r["source_file"]].append(r)
    for source, refs in sources.items():
        if source not in manifest:
            manual_only = all(r["origin"] == "manual" for r in refs)
            manifest[source] = {"source_file": source, "sha256": hashes.get(source, ""),
                                "status": "manual" if manual_only else "ok", "n_refs": str(len(refs)),
                                "processed_at": "", "message": "imported from an existing bibliography.csv"}

    events = defaultdict(list)

    # renamed PDFs: same fingerprint, new name
    known_hashes = {m["sha256"]: name for name, m in manifest.items() if m.get("sha256")}
    for name in list(by_name):
        old_name = known_hashes.get(hashes[name])
        if name not in manifest and old_name and old_name not in by_name:
            for r in records:
                if r["source_file"] == old_name:
                    r["source_file"] = name
            entry = manifest.pop(old_name)
            entry["source_file"] = name
            manifest[name] = entry
            events["renamed"].append(f"{old_name} -> {name}")

    # missing and changed PDFs
    for name, entry in list(manifest.items()):
        if name not in by_name:
            if args.prune_missing:
                records = [r for r in records if r["source_file"] != name]
                del manifest[name]
                events["pruned"].append(name)
            else:
                entry["status"] = "missing"
                events["missing"].append(name)
        elif entry["status"] == "missing" and entry.get("sha256") in ("", hashes[name]):
            entry["status"] = "ok" if any(r["source_file"] == name and r["origin"] in GENERATED_ORIGINS for r in records) else "manual"
            entry["sha256"] = hashes[name]
            events["back"].append(name)
        elif entry.get("sha256") and entry["sha256"] != hashes[name]:
            entry["status"] = "changed"
            events["changed"].append(name)
        elif not entry.get("sha256"):
            entry["sha256"] = hashes[name]

    refs_hashes = {p.name: sha256_of(refs_txt_path(p)) for p in pdf_files if refs_txt_path(p).exists()}
    to_process = []
    for p in pdf_files:
        entry = manifest.get(p.name)
        new_refs_txt = p.name in refs_hashes and (entry is None or entry.get("refs_txt_sha256") != refs_hashes[p.name])
        if entry is None or entry["status"] == "failed" or new_refs_txt:
            to_process.append(p)
        elif entry["status"] == "no_refs" and args.retry_empty:
            to_process.append(p)
        elif entry["status"] == "changed" and args.reprocess_changed:
            to_process.append(p)

    def save():
        write_csv_atomic(out_path, FIELDNAMES, records)
        write_csv_atomic(paths["manifest"], MANIFEST_FIELDNAMES, sorted(manifest.values(), key=lambda m: m["source_file"]))

    # ---- GROBID ----
    version = None
    interrupted = False
    processed = Counter()
    if to_process:
        try:
            version = grobid_version(args.grobid_url)
        except GrobidUnavailable as e:
            log(f"!! GROBID does not answer at {args.grobid_url} ({e}).")
            if sys.platform.startswith("win"):
                log("   On Windows, GROBID runs with Docker: see https://grobid.readthedocs.io (Docker section),")
                log("   or use a GROBID server running on another computer with --grobid-url http://<address>:8070.")
            else:
                log("   Start it (cd ~/grobid && ./gradlew run) or pass --grobid-url.")
            log("   Nothing was sent.")
            to_process = []
            events["grobid_down"].append(args.grobid_url)
        else:
            log(f"GROBID {version} at {args.grobid_url} (consolidation {args.consolidate})")
            if args.dump_raw:
                args.dump_raw.mkdir(parents=True, exist_ok=True)
    try:
        for i, pdf in enumerate(to_process, 1):
            log(f"[{i}/{len(to_process)}] {pdf.name}")
            entry = {"source_file": pdf.name, "sha256": hashes[pdf.name],
                     "processed_at": dt.datetime.now().isoformat(timespec="seconds")}
            diag = diagnose_pdf(pdf)
            entry.update(pages=str(diag["pages"]), chars_per_page=str(diag["chars_per_page"]),
                         biblio_heading=diag["biblio_heading"], producer=diag["producer"],
                         refs_txt_sha256=refs_hashes.get(pdf.name, ""))
            refs_txt = refs_txt_path(pdf)
            try:
                no_text = not diag["error"] and diag["chars_per_page"] < MIN_CHARS_PER_PAGE
                if pdf.name in refs_hashes:
                    # references copied by hand: GROBID only structures them
                    citations = read_citations(refs_txt)
                    tei = call_grobid_citations(citations, args.grobid_url, args.timeout, args.consolidate) if citations else ""
                    new_refs = parse_tei_references(tei, pdf.name, "citation_list", "c") if tei.strip() else []
                    for record, raw in zip(new_refs, citations):
                        record["raw_citation"] = record["raw_citation"] or raw
                    how = f"from {refs_txt.name}"
                    if not new_refs:
                        raise RuntimeError(f"GROBID parsed no reference from {refs_txt.name}: check that it contains "
                                           "the references as text, one per line or separated by an empty line")
                elif no_text:
                    new_refs = None
                    how = ""
                else:
                    tei = call_grobid(pdf, args.grobid_url, args.timeout, args.consolidate)
                    if args.dump_raw:
                        (args.dump_raw / f"{pdf.stem}.xml").write_text(tei, encoding="utf-8")
                    # GROBID answers with an empty body (HTTP 204) when it finds no reference
                    new_refs = parse_tei_references(tei, pdf.name, "grobid") if tei.strip() else []
                    how = ""
                if new_refs is None:
                    message = NO_TEXT_HINT.format(cpp=diag["chars_per_page"], name=refs_txt.name)
                    entry.update(status="no_text", n_refs="0", message=message)
                    processed["no_text"] += 1
                    log(f"    -> not sent to GROBID: {message}")
                else:
                    records = [r for r in records if not (r["source_file"] == pdf.name and r["origin"] in GENERATED_ORIGINS)]
                    records.extend(new_refs)
                    if new_refs:
                        message = ""
                    elif diag["biblio_heading"] == "yes" and pdf.name not in refs_hashes:
                        message = NOT_RECOGNISED_HINT.format(name=refs_txt.name)
                    else:
                        message = EMPTY_RESULT_HINT
                    entry.update(status="ok" if new_refs else "no_refs", n_refs=str(len(new_refs)), message=message)
                    processed["ok" if new_refs else "no_refs"] += 1
                    log(f"    -> {len(new_refs)} references {how}".rstrip() + (f" ({message})" if message else ""))
            except Exception as e:  # noqa: BLE001 - every failure is logged in the manifest
                entry.update(status="failed", n_refs="0", message=str(e)[:500])
                processed["failed"] += 1
                log(f"    !! failed: {str(e)[:200]}")
            manifest[pdf.name] = entry
            save()  # after each PDF: an interruption loses nothing
            if args.sleep:
                time.sleep(args.sleep)
    except KeyboardInterrupt:
        interrupted = True
        log("\nInterrupted: the PDFs already processed are saved.")

    # ---- recompute keys and outputs ----
    for r in records:
        r["dedup_key"] = compute_dedup_key(r)
    for name, entry in manifest.items():
        if entry["status"] in ("ok", "manual", "changed"):
            entry["n_refs"] = str(sum(1 for r in records if r["source_file"] == name))
    save()

    included = load_covidence(args.included)
    screened = load_covidence(args.screened)
    seed_index = build_seed_index(included + screened) if (included or screened) else None
    unique_rows = build_unique(records, seed_index, args.flag_grey)
    unique_fields = UNIQUE_FIELDNAMES + (["likely_grey", "grey_reason"] if args.flag_grey else [])
    write_csv_atomic(paths["unique"], unique_fields, unique_rows)

    checks = run_quality_checks(records, manifest, args.min_refs)
    if included:
        checks.extend(check_pdf_names(list(by_name), included))
    same_hash = defaultdict(list)
    for name, digest in hashes.items():
        same_hash[digest].append(name)
    for names in same_hash.values():
        if len(names) > 1:
            for name in names:
                checks.append(check_row("high", "PDF_IDENTICAL_FILES", {"source_file": name},
                                        f"Same content as {', '.join(n for n in names if n != name)}: references counted twice"))
    orphans = write_checks(paths["checks"], checks)

    grey_stats = write_grey_validation(paths["grey"], unique_rows) if args.flag_grey else None

    rules_text = rules_description()
    paths["readme"].write_text(README_TEXT.format(version=SCRIPT_VERSION, date=started.date().isoformat(),
                                                  kept=BACKUPS_KEPT, rules=rules_text), encoding="utf-8")

    # ---- statistics ----
    status_count = Counter(m["status"] for m in manifest.values())
    n_manual = sum(1 for r in records if r["origin"] == "manual")
    origin_count = Counter(r["origin"] for r in records)
    n_nokey = sum(1 for r in records if not r["dedup_key"])
    n_doi = sum(1 for u in unique_rows if u["doi"])
    n_seed = sum(1 for u in unique_rows if u["in_seed_corpus"])
    n_grey = sum(1 for u in unique_rows if u.get("likely_grey") == "yes")
    check_count = Counter((c["severity"], c["check"]) for c in checks)
    n_high = sum(n for (sev, _), n in check_count.items() if sev == "high")
    n_open = sum(1 for c in checks if not (c.get("verified") or "").strip())

    run = {
        "date": started.isoformat(timespec="seconds"), "script_version": SCRIPT_VERSION,
        "grobid_url": args.grobid_url, "grobid_version": version, "consolidate": args.consolidate,
        "arguments": {k: str(v) for k, v in vars(args).items()}, "interrupted": interrupted,
        "pdfs_in_folder": len(pdf_files), "processed_now": dict(processed),
        "manifest_status": dict(status_count), "events": {k: v for k, v in events.items()},
        "failures": {m["source_file"]: m["message"] for m in manifest.values() if m["status"] == "failed"},
        "references": len(records), "manual_references": n_manual, "without_key": n_nokey,
        "unique_references": len(unique_rows), "unique_with_doi": n_doi,
        "already_in_seed_corpus": n_seed if seed_index else None,
        "grey_flagged": n_grey if args.flag_grey else None, "grey_validation": grey_stats,
        "checks": {f"{s}:{c}": n for (s, c), n in check_count.items()},
        "grey_rules": rules_text if args.flag_grey else None,
    }
    history = []
    if paths["log"].exists():
        try:
            history = json.loads(paths["log"].read_text(encoding="utf-8"))
        except ValueError:
            history = []
    history.append(run)
    paths["log"].write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- console summary ----
    def line(label, value, note=""):
        log(f"  {label:.<30} {value:>6}   {note}".rstrip())

    log("")
    log(f"extract_bibliography.py {SCRIPT_VERSION}")
    if version:
        log(f"GROBID {version} at {args.grobid_url} (consolidation {args.consolidate})")
    log("")
    log("PDFs")
    line("in folder", len(pdf_files))
    line("processed now", sum(processed.values()),
         f"({processed['ok']} ok, {processed['no_refs']} without reference, {processed['no_text']} without text, "
         f"{processed['failed']} failed)")
    for name, message in run["failures"].items():
        log(f"      failed: {name}: {message[:120]}")
    not_recognised = [m["source_file"] for m in manifest.values()
                      if m["status"] == "no_refs" and m.get("biblio_heading") == "yes" and not m.get("refs_txt_sha256")]
    line("without text layer", status_count["no_text"], "-> original PDF, or <name>.refs.txt" if status_count["no_text"] else "")
    line("bibliography not recognised", len(not_recognised), "-> copy the references into <name>.refs.txt" if not_recognised else "")
    line("without reference", status_count["no_refs"] - len(not_recognised),
         "(no bibliography heading found)" if status_count["no_refs"] - len(not_recognised) else "")
    line("typed by hand only", status_count["manual"])
    for key, label, hint in (("renamed", "renamed (rows updated)", ""),
                             ("changed", "changed since extraction",
                              "(re-extracted)" if args.reprocess_changed else "-> --reprocess-changed"),
                             ("missing", "in CSV but PDF missing", "-> --prune-missing"),
                             ("pruned", "removed (--prune-missing)", ""),
                             ("back", "back in the folder", "")):
        if events[key]:
            line(label, len(events[key]), hint)
            for item in events[key]:
                log(f"      {item}")
    log("")
    log("References")
    line("extracted", len(records), "(" + ", ".join(f"{origin}: {n}" for origin, n in sorted(origin_count.items())) + ")")
    line("without DOI nor title", n_nokey, "(not in the unique file)")
    line("unique", len(unique_rows), f"({n_doi} with DOI)")
    if seed_index:
        line("already in Covidence exports", n_seed, "(PRISMA: already identified)")
    if args.flag_grey:
        line("flagged as grey literature", n_grey, "(pre-screening, not a decision)")
        if grey_stats:
            f_, n_ = grey_stats["flagged"], grey_stats["not_flagged"]
            line("grey validation sample", grey_stats["sample_size"],
                 f"({f_['reviewed']}/{f_['in_sample']} flagged and {n_['reviewed']}/{n_['in_sample']} not flagged reviewed)")
            if f_["reviewed"]:
                fp = f_["reviewed"] - f_["confirmed_grey"]
                log(f"      false positives: {fp}/{f_['reviewed']} ({fp / f_['reviewed']:.0%}) of the flagged references are not grey")
            if n_["reviewed"]:
                fn = n_["confirmed_grey"]
                log(f"      false negatives: {fn}/{n_['reviewed']} ({fn / n_['reviewed']:.0%}) of the not flagged references are grey")
    log("")
    log("Checks to verify")
    for (sev, check), n in sorted(check_count.items(), key=lambda kv: (SEVERITY_ORDER[kv[0][0]], kv[0][1])):
        log(f"  {sev:<6} {check:<28} {n:>5}")
    log(f"  {'':<6} {'not yet verified':<28} {n_open:>5}")
    if orphans:
        log(f"  !! {orphans} annotation(s) of the previous checks file no longer match a check (problem fixed?)")
    log("")
    log(f"Files (in {args.output_dir})")
    log(f"  {out_path.name:<34} working file (backup: {backup_path.relative_to(out_path.parent) if backup_path else 'none, first run'})")
    log(f"  {paths['unique'].name:<34} screening candidates")
    log(f"  {paths['checks'].name:<34} {len(checks)} rows, {n_high} high priority")
    if args.flag_grey:
        log(f"  {paths['grey'].name:<34} 10 % sample to review")
    log(f"  {paths['readme'].name:<34} what each file is and what to do")
    log(f"  {paths['log'].name:<34} GROBID version and settings, for the methods section")
    log("")
    log("Next steps")
    step = 1
    if events["grobid_down"]:
        log(f"  {step}. Start GROBID, then rerun."); step += 1
    if status_count["no_text"]:
        log(f"  {step}. PDFs without text layer: get the original PDF, or type their references into"
            f" <name>{REFS_TXT_SUFFIX} next to the PDF (screenshot text recognition: see {paths['readme'].name})."); step += 1
    if not_recognised:
        log(f"  {step}. Bibliographies not recognised by GROBID (posters, short papers): copy the references into"
            f" <name>{REFS_TXT_SUFFIX} next to the PDF (one per line, or separated by an empty line), then rerun."); step += 1
    if status_count["failed"]:
        log(f"  {step}. Check the failed PDFs (message above and in {paths['manifest'].name}), then rerun."); step += 1
    if n_open:
        log(f"  {step}. Review {paths['checks'].name}, high priority first; fill 'verified' and 'decision'."); step += 1
    if args.flag_grey and grey_stats and (grey_stats["flagged"]["reviewed"] + grey_stats["not_flagged"]["reviewed"]) < grey_stats["sample_size"]:
        log(f"  {step}. Fill reviewer_is_grey in {paths['grey'].name} (instructions in {paths['readme'].name})."); step += 1
    log(f"  {step}. After any edit of {out_path.name}, rerun the script to refresh every file.")
    if interrupted:
        sys.exit(130)


if __name__ == "__main__":
    main()
