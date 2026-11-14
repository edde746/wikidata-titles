"""Build the wikidata-titles release files: the names, IDs, years, sizes and relations of every Wikidata item with a
TMDB, AniDB, MyAnimeList or AniList ID, as Trave files (https://github.com/edde746/trave, magic WDTT): the core,
wikidata-titles.bin.gz (items and their English, Japanese and "mul" names and titles), and one
wikidata-names-<script>.bin.gz per script with the labels and aliases in every other language.

The data comes from a Wikidata SPARQL endpoint (QLever's by default), fetched in pages into an upstream directory of
TSV files, one per query. The build reads only that directory, so the same fetch always gives the same bytes.

Usage:
  python build.py --fetch UPSTREAM_DIR             fetch the queries' results into UPSTREAM_DIR
  python build.py UPSTREAM_DIR OUT_DIR [--check]   build the files (gzip) into OUT_DIR, and with --check read every
                                                   one back and compare it row by row with UPSTREAM_DIR
  python build.py --dump FILE                      print FILE's tables and columns with their sizes
Python 3.9 or newer, plus trave (pip install -r requirements.txt).
"""
import argparse
import hashlib
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import trave

MAGIC = b"WDTT"

ENDPOINT = os.environ.get("WIKIDATA_SPARQL", "https://qlever.dev/api/wikidata")
# Wikimedia's User-Agent policy: name the tool and a way to reach its operator.
USER_AGENT = os.environ.get(
    "WIKIDATA_USER_AGENT", "wikidata-titles/1 (+https://github.com/OWNER/wikidata-titles; build.py)")
PAGE = 400_000  # rows per request: QLever's public instance stops a query after 30 s

ENTITY = "http://www.wikidata.org/entity/Q"
DIRECT = "http://www.wikidata.org/prop/direct/"

# The items: every item with one of these identifiers, and every film, series, season or OVA (WORK_CLASSES, by its
# P31) that is part of a series (P179) with a TMDB TV ID: seasons and cours, which rarely have a TMDB entry of their
# own. Episodes are left out.
ID_PROPERTIES = {"P4947": "tmdb_movie", "P4983": "tmdb_tv", "P5646": "anidb", "P4086": "mal", "P8729": "anilist"}
WORK_CLASSES = (
    11424, 506240, 24862, 17517379, 202866, 1261214, 20650540, 226730, 20667187,  # films and specials
    5398426, 15416, 63952888, 117467246, 581714, 1259759, 526877, 117467240,  # series
    113687694, 113671041, 220898, 1047299,  # OVA and ONA (series)
    100269041, 104775758, 117209498, 29982285, 24856, 98701476, 125354488, 653916,  # seasons, film series, ...
)
ITEMS = ("{ SELECT DISTINCT ?item WHERE { { VALUES ?sp { %s } ?item ?sp ?sx } UNION { VALUES ?sc { %s } "
         "?item wdt:P31 ?sc . ?item wdt:P179 ?series . ?series wdt:P4983 ?st } } }"
         % (" ".join("wdt:" + p for p in ID_PROPERTIES), " ".join(f"wd:Q{c}" for c in WORK_CLASSES)))
PREFIXES = """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX p: <http://www.wikidata.org/prop/>
PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
"""
# One query per upstream file: (file, projected variables, pattern).
QUERIES = (
    ("ids", "?item ?p ?v",
     ITEMS + " VALUES ?p { %s wdt:P31 wdt:P179 } ?item ?p ?v" % " ".join("wdt:" + p for p in ID_PROPERTIES)),
    ("labels", "?item ?t", ITEMS + " ?item rdfs:label ?t"),
    ("aliases", "?item ?t", ITEMS + " ?item skos:altLabel ?t"),
    ("titles", "?item ?p ?t", ITEMS + " VALUES ?p { wdt:P1476 wdt:P1705 wdt:P2125 } ?item ?p ?t"),
    ("romanized", "?item ?t", ITEMS + " { ?item p:P1476 ?st } UNION { ?item p:P1705 ?st } ?st pq:P2125 ?t"),
    ("dates", "?item ?p ?d", ITEMS + " VALUES ?p { wdt:P577 wdt:P580 } ?item ?p ?d"),
    ("facts", "?item ?p ?v", ITEMS + " VALUES ?p { wdt:P1113 wdt:P2437 wdt:P582 wdt:P155 wdt:P156 } ?item ?p ?v"),
    # The English labels of the items' classes, for `kind` and `anime`.
    ("classes", "?c ?l", "{ SELECT DISTINCT ?c ?l WHERE { " + ITEMS +
     ' ?item wdt:P31 ?c . ?c rdfs:label ?l FILTER (LANG(?l) = "en") } }'),
)

# Labels and aliases are kept in these languages (and their variants, en-gb, ja-hira, ...): English, Japanese and
# "mul", Wikidata's language-independent label (romanised titles of anime often live there). Title (P1476), native
# label (P1705) and Hepburn romanisation (P2125) are kept in every language.
LABEL_LANGUAGES = ("en", "ja", "mul")
# Name sources: Wikidata's label and aliases, and three properties.
SOURCES = ("label", "alias", "P1476", "P1705", "P2125")

# Labels and aliases in the other languages go to one optional table per script (`names_<script>`), by the script
# most of the text's letters are in (the first word of their Unicode character names, the second after FULLWIDTH or
# HALFWIDTH); text without letters goes to "other".
SCRIPTS = ("latin", "cyrillic", "cjk", "arabic", "other")
SCRIPT_OF = {"LATIN": "latin", "CYRILLIC": "cyrillic", "CJK": "cjk", "HANGUL": "cjk", "HIRAGANA": "cjk",
             "KATAKANA": "cjk", "BOPOMOFO": "cjk", "IDEOGRAPHIC": "cjk", "ARABIC": "arabic"}

# Item kinds, from the English labels of the item's classes (P31), the first rule that matches any class in this
# order; a TMDB TV ID makes it a series and a TMDB movie ID a film whatever the classes say.
KINDS = (("episode", ("episode",)), ("season", ("season",)),
         ("series", ("series", "television program", "serial")), ("film", ("film", "movie")))

DECIMAL = re.compile(r"[1-9][0-9]*")
INTEGER = re.compile(r"\+?([0-9]+)(\.0*)?")


class Unknown(Exception):
    """Upstream data the builder doesn't know: a value of a shape it doesn't handle."""


# --- fetching --------------------------------------------------------------------------------------------------------

def sparql(query):
    """One query's TSV, retried; QLever reports a timeout by appending an error to a cut-short result."""
    body = urllib.parse.urlencode({"query": query}).encode()
    last = None
    for attempt in range(5):
        if attempt:
            time.sleep(5 * attempt * attempt)
        request = urllib.request.Request(ENDPOINT, data=body, headers={
            "Accept": "text/tab-separated-values", "Accept-Encoding": "gzip", "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
        except (urllib.error.URLError, OSError) as e:
            last = e
            print(f"  attempt {attempt + 1}: {e}", file=sys.stderr, flush=True)
            continue
        if b"!!!!>># An error" in data:
            last = RuntimeError("the endpoint cut the result short (timeout)")
            print(f"  attempt {attempt + 1}: {last}", file=sys.stderr, flush=True)
            continue
        return data.decode("utf-8")
    raise SystemExit(f"query failed: {last}")


def fetch(directory):
    """Each query's rows into DIRECTORY/<name>.tsv (header line first), counted first and then paged."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, variables, pattern in QUERIES:
        started = time.time()
        count = sparql(PREFIXES + f"SELECT (COUNT(*) AS ?n) WHERE {{ {pattern} }}").split("\n")[1]
        total = int(term(count)[1])
        lines = []
        offset = 0
        while True:
            page = sparql(PREFIXES + f"SELECT {variables} WHERE {{ {pattern} }} LIMIT {PAGE} OFFSET {offset}")
            rows = page.split("\n")[1:]
            if rows and rows[-1] == "":
                rows.pop()
            lines += rows
            offset += PAGE
            if len(rows) < PAGE:
                break
        # The index updates live: a count and its pages may differ by a few rows.
        if abs(len(lines) - total) > total // 1000:
            raise SystemExit(f"{name}: {len(lines)} rows, the count said {total}")
        tmp = directory / f"{name}.tsv.part"
        tmp.write_text("\t".join(variables.split()) + "\n" + "".join(l + "\n" for l in lines), encoding="utf-8")
        os.replace(tmp, directory / f"{name}.tsv")
        print(f"fetched {name}: {len(lines):,} rows (counted {total:,}) in {time.time() - started:.0f} s", flush=True)


# --- upstream rows ---------------------------------------------------------------------------------------------------

GENID = "http://www.wikidata.org/.well-known/genid/"


def term(cell):
    """A TSV cell as ("iri", iri) or ("literal", text, language, datatype); None for a blank node (Wikidata's
    "unknown value", a genid IRI in the RDF export).

    QLever's TSV doesn't escape quotes or backslashes in a literal, and Wikidata's strings hold no tab or newline:
    a literal runs from the first quote to the last."""
    if cell.startswith("<") and cell.endswith(">"):
        return None if cell[1:].startswith(GENID) else ("iri", cell[1:-1])
    if cell.startswith('"'):
        end = cell.rfind('"')
        if end < 1:
            raise Unknown(f"unterminated literal {cell!r}")
        rest = cell[end + 1:]
        lang = rest[1:] if rest.startswith("@") else None
        dtype = rest[3:-1] if rest.startswith("^^<") else None
        return ("literal", cell[1:end], lang, dtype)
    if cell.startswith("_:"):
        return None
    return ("literal", cell, None, None)


def rows(directory, name):
    """A file's rows as lists of terms; rows with a blank node (Wikidata's "unknown value") are skipped."""
    with open(directory / f"{name}.tsv", encoding="utf-8") as f:
        next(f)
        for n, line in enumerate(f, 2):
            cells = [term(c) for c in line.rstrip("\n").split("\t")]
            if any(c is None for c in cells):
                continue
            yield n, cells


def qid(t, where):
    if t[0] != "iri" or not t[1].startswith(ENTITY) or not DECIMAL.fullmatch(t[1][len(ENTITY):]):
        raise Unknown(f"{where}: {t!r} is not an item")
    return int(t[1][len(ENTITY):])


def prop(t, where):
    if t[0] != "iri" or not t[1].startswith(DIRECT):
        raise Unknown(f"{where}: {t!r} is not a property")
    return t[1][len(DIRECT):]


def year(t):
    """The year of a time value (xsd:dateTime, "-0100-01-01T00:00:00Z" for 101 BC), None if it has none."""
    if t[0] != "literal":
        return None
    m = re.match(r"(-?)0*([0-9]+)-", t[1])
    if not m:
        return None
    return -int(m.group(2)) if m.group(1) else int(m.group(2))


def keep_language(lang):
    return lang is not None and any(lang == l or lang.startswith(l + "-") for l in LABEL_LANGUAGES)


def script(text):
    """The script most of the text's letters are in (SCRIPTS), "other" for text without letters."""
    counts = {}
    for ch in text:
        if ch.isalpha():
            words = unicodedata.name(ch, "").split(" ", 2)
            word = words[1] if words[0] in ("FULLWIDTH", "HALFWIDTH") and len(words) > 1 else words[0]
            s = SCRIPT_OF.get(word, "other")
            counts[s] = counts.get(s, 0) + 1
    if not counts:
        return "other"
    return max(SCRIPTS, key=lambda s: (counts.get(s, 0), -SCRIPTS.index(s)))


def kind_of(it, class_labels):
    if "tmdb_tv" in it:
        return "series"
    if "tmdb_movie" in it:
        return "film"
    labels = [class_labels[c].lower() for c in it.get("p31", ()) if c in class_labels]
    for kind, words in KINDS:
        if any(w in l for l in labels for w in words):
            return kind
    return "other"


def upstream(directory):
    """The dataset from the upstream files: ({qid: item}, [(qid, source, language, text)], {script: [names]}), all
    sorted; the last is the labels and aliases in other languages, by script."""
    items = {}

    def item(q):
        it = items.get(q)
        if it is None:
            it = items[q] = {"qid": q}
        return it

    for n, (s, p, v) in rows(directory, "ids"):
        where = f"ids.tsv:{n}"
        it, p = item(qid(s, where)), prop(p, where)
        if p in ID_PROPERTIES:
            if v[0] != "literal" or not DECIMAL.fullmatch(v[1]):
                continue  # a malformed identifier (Wikidata flags them; there are a few)
            it.setdefault(ID_PROPERTIES[p], set()).add(int(v[1]))
        elif p in ("P31", "P179"):
            it.setdefault(p.lower(), set()).add(qid(v, where))
        else:
            raise Unknown(f"{where}: unexpected property {p}")
    for n, (s, p, d) in rows(directory, "dates"):
        y = year(d)
        if y is None:
            continue
        it = item(qid(s, f"dates.tsv:{n}"))
        key = {"P577": "published", "P580": "start"}[prop(p, f"dates.tsv:{n}")]
        it[key] = min(it.get(key, y), y)
    for n, (s, p, v) in rows(directory, "facts"):
        where = f"facts.tsv:{n}"
        it, p = item(qid(s, where)), prop(p, where)
        if p in ("P1113", "P2437"):
            m = INTEGER.fullmatch(v[1]) if v[0] == "literal" else None
            if not m or int(m.group(1)) == 0:
                continue  # a fraction, a range or zero: not a count
            key = "episodes" if p == "P1113" else "seasons"
            it[key] = max(it.get(key, 0), int(m.group(1)))
        elif p == "P582":
            y = year(v)
            if y is not None:
                it["end"] = max(it.get("end", y), y)
        elif p in ("P155", "P156"):
            if v[0] == "iri" and v[1].startswith(ENTITY) and DECIMAL.fullmatch(v[1][len(ENTITY):]):
                it.setdefault("follows" if p == "P155" else "followed_by", set()).add(int(v[1][len(ENTITY):]))
        else:
            raise Unknown(f"{where}: unexpected property {p}")
    class_labels = {}
    for n, (c, l) in rows(directory, "classes"):
        if l[0] == "literal":
            q = qid(c, f"classes.tsv:{n}")
            class_labels[q] = min(class_labels.get(q, l[1]), l[1])

    names, label_langs, other = set(), {}, {}
    for source, name in (("label", "labels"), ("alias", "aliases"), ("P2125", "romanized")):
        for n, (s, t) in rows(directory, name):
            if t[0] != "literal":
                continue
            q = qid(s, f"{name}.tsv:{n}")
            if source == "label" and t[2]:
                label_langs.setdefault(q, set()).add(t[2])
            if source == "P2125" or keep_language(t[2]):
                names.add((q, source, t[2] or "", t[1]))
            elif t[2] and t[1]:
                # one row per distinct text of an item: the label before the alias, then the first language
                k, v = (q, t[1]), (SOURCES.index(source), t[2])
                if k not in other or v < other[k]:
                    other[k] = v
    for n, (s, p, t) in rows(directory, "titles"):
        if t[0] == "literal":
            names.add((qid(s, f"titles.tsv:{n}"), prop(p, f"titles.tsv:{n}"), t[2] or "", t[1]))

    # The scope: an item with an identifier, or a part of a series with a TMDB TV ID. Rows of other items (a page
    # boundary moved by a live update, a P179 target fetched for its IDs) are left out.
    def in_scope(it):
        return any(k in it for k in ID_PROPERTIES.values()) or (
            any(c in WORK_CLASSES for c in it.get("p31", ())) and
            any("tmdb_tv" in items.get(s, {}) for s in it.get("p179", ())))

    kept = {q: it for q, it in sorted(items.items()) if in_scope(it)}
    for q, it in kept.items():
        it["kind"] = kind_of(it, class_labels)
        if any(k in it for k in ("anidb", "mal", "anilist")) or any(
                "anime" in class_labels.get(c, "").lower() for c in it.get("p31", ())):
            it["anime"] = 1
        if q in label_langs:
            it["label_langs"] = len(label_langs[q])
    names = sorted((n for n in names if n[0] in kept and n[3] != ""), key=name_order)
    # Other-language texts an item already has as a name are left out.
    have = {(n[0], n[3]) for n in names}
    shards = {s: [] for s in SCRIPTS}
    for (q, text), (source, lang) in other.items():
        if q in kept and (q, text) not in have:
            shards[script(text)].append((q, SOURCES[source], lang, text))
    for s in SCRIPTS:
        shards[s].sort(key=name_order)
    return kept, names, shards


def name_order(n):
    q, source, lang, text = n
    return (q, SOURCES.index(source), lang, text)


# The items table's list columns and number columns, in the file's order.
ITEM_LISTS = ("tmdb_movie", "tmdb_tv", "anidb", "mal", "anilist", "p31", "p179")
ITEM_INTS = ("published", "start")
# Added later in the file's order (after the columns above; readers that don't know them skip them).
ITEM_FACTS_INTS = ("end", "episodes", "seasons", "label_langs", "anime")
ITEM_FACTS_LISTS = ("follows", "followed_by")


def tables(items, names, shards):
    order = list(items)
    row = {q: i for i, q in enumerate(order)}

    def listed(key):
        return [sorted(items[q][key]) if key in items[q] else None for q in order]

    item_columns = [("qid", trave.INT_DELTA, order)]
    item_columns += [(key, trave.INT_LIST, listed(key)) for key in ITEM_LISTS]
    item_columns += [(key, trave.INT, [items[q].get(key) for q in order]) for key in ITEM_INTS]
    item_columns += [("kind", trave.DICT, [items[q]["kind"] for q in order])]
    item_columns += [(key, trave.INT, [items[q].get(key) for q in order]) for key in ITEM_FACTS_INTS]
    item_columns += [(key, trave.INT_LIST, listed(key)) for key in ITEM_FACTS_LISTS]

    def name_columns(names):
        return [
            ("item", trave.INT_DELTA, [row[n[0]] for n in names]),
            ("source", trave.DICT, [n[1] for n in names]),
            ("lang", trave.DICT, [n[2] or None for n in names]),
            ("text", trave.STR, [n[3] for n in names]),
        ]

    core = [("items", len(order), item_columns), ("names", len(names), name_columns(names))]
    return core, {s: [(f"names_{s}", len(shards[s]), name_columns(shards[s]))] for s in SCRIPTS}


# The release's files: the core (items and names) and one file per script (its names_<script> table), named here.
CORE_FILE = "wikidata-titles.bin.gz"


def script_file(s):
    return f"wikidata-names-{s}.bin.gz"


def build_table(build_id):
    """The `build` table: one row naming the core build (the SHA-256 of the core's items and names tables as packed,
    before this table is added), in every file, so that a client can tell that a script file goes with its core."""
    return ("build", 1, [("id", trave.STR, [build_id])])


def build(directory):
    """The upstream directory as {file name: Trave bytes (not gzipped)}: the core and one file per script."""
    core, scripts = tables(*upstream(directory))
    build_id = hashlib.sha256(trave.pack(MAGIC, core)).hexdigest()
    out = {CORE_FILE: trave.pack(MAGIC, [build_table(build_id)] + core)}
    for s in SCRIPTS:
        out[script_file(s)] = trave.pack(MAGIC, [build_table(build_id)] + scripts[s])
    return out


def build_id(tables_read, where):
    """The file's `build` table's id."""
    if "build" not in tables_read or tables_read["build"][0] != 1:
        raise ValueError(f"{where}: no build table")
    return tables_read["build"][1]["id"][0]


def dataset(core_read, scripts_read):
    """The core's tables and each script file's tables back as ({qid: item}, [(qid, source, language, text)],
    {script: [names]}), the shapes `upstream` returns. A script file of another build is an error: its `item` rows
    point into another core's items."""
    core_id = build_id(core_read, CORE_FILE)
    nitems, ic = core_read["items"]
    items = {}
    for i in range(nitems):
        it = {"qid": ic["qid"][i], "kind": ic["kind"][i]}
        for key in ITEM_LISTS + ITEM_FACTS_LISTS:
            if ic[key][i] is not None:
                it[key] = set(ic[key][i])
        for key in ITEM_INTS + ITEM_FACTS_INTS:
            if ic[key][i] is not None:
                it[key] = ic[key][i]
        items[it["qid"]] = it
    order = ic["qid"]

    def names(tables_read, table):
        _, nc = tables_read[table]
        return [(order[i], s, l or "", t) for i, s, l, t in zip(nc["item"], nc["source"], nc["lang"], nc["text"])]

    shards = {}
    for s, t in scripts_read.items():
        if build_id(t, script_file(s)) != core_id:
            raise ValueError(f"{script_file(s)} is of build {build_id(t, script_file(s))}, the core of {core_id}")
        shards[s] = names(t, f"names_{s}")
    return items, names(core_read, "names"), shards


# --- command line ----------------------------------------------------------------------------------------------------

def check(out, directory):
    """Reads every file in OUT back and compares it with UPSTREAM_DIR, and checks that the build ids agree (they
    are the SHA-256 of the core's items and names, recomputed here)."""
    core_read = trave.read((out / CORE_FILE).read_bytes(), MAGIC)
    scripts_read = {s: trave.read((out / script_file(s)).read_bytes(), MAGIC) for s in SCRIPTS}
    actual_items, actual_names, actual_shards = dataset(core_read, scripts_read)
    items, names, shards = upstream(directory)
    core, _ = tables(items, names, shards)
    if build_id(core_read, CORE_FILE) != hashlib.sha256(trave.pack(MAGIC, core)).hexdigest():
        raise SystemExit("check: the core's build id is not the SHA-256 of its items and names")
    if actual_items != items:
        for q in items:
            if actual_items.get(q) != items[q]:
                raise SystemExit(f"check: item Q{q} reads back as {actual_items.get(q)}, upstream {items[q]}")
        raise SystemExit(f"check: {len(actual_items)} items read back, {len(items)} upstream")
    for table, actual, expected in [("names", actual_names, names)] + [
            (f"names_{s}", actual_shards[s], shards[s]) for s in SCRIPTS]:
        if actual != expected:
            for a, x in zip(actual, expected):
                if a != x:
                    raise SystemExit(f"check: {table}: name {a} read back, upstream {x}")
            raise SystemExit(f"check: {table}: {len(actual)} names read back, {len(expected)} upstream")
    other = sum(len(s) for s in shards.values())
    print(f"checked {1 + len(SCRIPTS)} files of build {build_id(core_read, CORE_FILE)[:12]}: {len(items):,} items, "
          f"{len(names):,} names and {other:,} other-language names read back the same", flush=True)


def dump(path):
    data = Path(path).read_bytes()
    raw = trave.unpack(data)
    trave.read(raw, MAGIC)  # the whole file is valid before anything is printed
    print(f"{path}: {len(data):,} bytes, {len(raw):,} unpacked")
    r = trave.Reader(raw, 5)
    for _ in range(r.varint()):
        name, nrows = r.text(), r.varint()
        print(f"{name}: {nrows:,} rows")
        for _ in range(r.varint()):
            cname, kind, length = r.text(), r.byte(), r.varint()
            value = r.bytes(length)
            shown = ""
            if name == "build" and kind == trave.STR:
                shown = "  " + trave.read_column(trave.STR, nrows, trave.Reader(value))[0][:12]
            print(f"  {cname:12} {trave.TYPE_NAMES.get(kind, kind):10} {length:>11,} bytes{shown}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", nargs="*", type=Path)
    p.add_argument("--fetch", type=Path, metavar="UPSTREAM_DIR", help="fetch the upstream files into UPSTREAM_DIR")
    p.add_argument("--check", action="store_true", help="read OUT_DIR's files back and compare them with UPSTREAM_DIR")
    p.add_argument("--dump", type=Path, help="print a Trave file's tables and columns")
    a = p.parse_args()
    if a.fetch:
        fetch(a.fetch)
        return
    if a.dump:
        dump(a.dump)
        return
    if len(a.inputs) != 2:
        p.error("UPSTREAM_DIR OUT_DIR expected")
    directory, out = a.inputs
    try:
        files = build(directory)
    except Unknown as e:
        raise SystemExit(f"unknown upstream data, not built: {e}")
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        packed = trave.gzip_bytes(data)
        tmp = out / (name + ".part")
        tmp.write_bytes(packed)
        os.replace(tmp, out / name)
        print(f"built {out / name}: {len(packed):,} bytes ({len(data):,} unpacked)", flush=True)
    if a.check:
        check(out, directory)


if __name__ == "__main__":
    sys.exit(main())
