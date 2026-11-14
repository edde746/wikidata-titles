# wikidata-titles

The titles of every film and series Wikidata links to TMDB, AniDB, MyAnimeList or AniList, in gzipped files rebuilt
every week:

- every Wikidata item with a TMDB movie (P4947) or TV series (P4983) ID, an AniDB (P5646), MyAnimeList (P4086) or
  AniList (P8729) anime ID, and every film, series, season or OVA that is part of a series (P179) with a TMDB TV
  ID (about 363,000 items);
- their IDs, their classes (P31), their series (P179), their first publication (P577), start (P580) and end (P582)
  years, their number of episodes (P1113) and seasons (P2437), the items they follow (P155) and are followed by
  (P156), and the number of languages they have a label in;
- their kind (film, series, season, episode or other) and whether they are anime, worked out from their classes'
  English labels and their IDs;
- their names: labels and aliases in English, Japanese and `mul` (with their variants: `en-gb`, `ja-hira`, ...),
  and their title (P1476), native label (P1705) and Hepburn romanisation (P2125) in every language (about 800,000
  names);
- their labels and aliases in every other language, one optional file per script (Latin, Cyrillic, CJK, Arabic and
  other; about 1,240,000 names), for apps that match names in those languages.

The files hold them, as Wikidata has them, as [Trave](https://github.com/edde746/trave) files: tables of named,
typed columns that an app reads with nothing but a gzip decoder. `wikidata-titles.bin.gz`, the core, has the items
and the names above (about 10.5 MB; 28 MB unpacked). `wikidata-names-<script>.bin.gz` has one script's
other-language names (1.2 to 8.4 MB each); an app downloads only the scripts it needs. Normalising the names for
lookups is up to the app, so it can change how it does without a new file.

It was made for the `wikidata` metadata provider of the kigumi media server, which resolves a folder or file name to
its TMDB entry when the name is a Wikidata label or alias of exactly one film or series.

## The data is Wikidata's

Every value comes from [Wikidata](https://www.wikidata.org/), unchanged; Wikidata's data is available under
[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) (see
[Wikidata:Licensing](https://www.wikidata.org/wiki/Wikidata:Licensing)). No attribution is required; please credit
"Contains data from Wikidata" anyway. Fix a wrong or missing title or ID on Wikidata itself: the next release picks
it up.

The TMDB, AniDB, MyAnimeList and AniList IDs are Wikidata's statements, not data from those services.

The code in this repository (the builder, the workflow) is under the [MIT licence](LICENSE); the licence covers the
code, not the data.

## Releases

A [workflow](.github/workflows/release.yml) runs every Monday and on demand. It fetches the data from the Wikidata
SPARQL endpoint of [QLever](https://qlever.dev/wikidata) (a full Wikidata index updated live), builds the files, reads
every one back and compares it row by row with what was fetched (`build.py --check`), and publishes a release tagged
`vYYYY.MM.DD` (`vYYYY.MM.DD.2`, `.3`, ... for a second release the same day, UTC) unless the latest release has the
same files. The release notes name the endpoint's index and the `build.py` it was built with. The 8 newest releases
are kept; older ones are deleted.

| Asset | Contents |
|---|---|
| `wikidata-titles.bin.gz` | the core: `items` and `names` (about 10.5 MB) |
| `wikidata-names-latin.bin.gz` | `names_latin`: labels and aliases in Latin script (about 8.4 MB) |
| `wikidata-names-cyrillic.bin.gz` | `names_cyrillic` (about 1.7 MB) |
| `wikidata-names-cjk.bin.gz` | `names_cjk`: Chinese, Korean, ... (about 2.0 MB) |
| `wikidata-names-arabic.bin.gz` | `names_arabic` (about 1.2 MB) |
| `wikidata-names-other.bin.gz` | `names_other`: every other script (about 1.6 MB) |
| `SHA256SUMS` | their SHA-256s, as `sha256sum` writes them |

A script file's names point at rows of the core's `items`, so it only goes with the core of the same build: every
file has a [`build`](#build) table naming it, and an app checks that they match. Take every file from one release.
To keep a copy up to date, ask for the latest release's tag and download only when it moved, every file from that
tag, so that a release published in between can't pair one release's checksum or core with another's file:

```sh
repo=https://github.com/edde746/wikidata-titles
tag=$(curl -sI "$repo/releases/latest" | sed -n 's|^location: .*/releases/tag/\([^[:space:]]*\).*|\1|ip')
for f in wikidata-titles.bin.gz wikidata-names-cyrillic.bin.gz SHA256SUMS; do   # the core, the scripts you want
  curl -LO "$repo/releases/download/$tag/$f"
done
sha256sum -c --ignore-missing SHA256SUMS
```

`releases/latest` answers with a redirect to `releases/tag/<tag>`; the check is that one request, without following
it. The same fetched data always gives the same bytes (`gzip -9`, no file name, no time).

## Building

```sh
python build.py --fetch upstream                  # the queries' results, one TSV per query (about 3 min)
python build.py upstream out --check              # build every file into out/, read each back and compare
python build.py --dump out/wikidata-titles.bin.gz # a file's tables and columns, with their sizes
```

Python 3.9 or newer, plus `trave` (`pip install -r requirements.txt`). `--fetch` sends a descriptive User-Agent (set
`WIKIDATA_USER_AGENT` to yours), pages each query (`LIMIT`/`OFFSET` after a `COUNT`, since QLever's public instance
stops a query after 30 s), and retries pages the endpoint cuts short. `WIKIDATA_SPARQL` points it at another
endpoint.

`dataset(core, scripts)` in `build.py` turns the core's and the script files' tables, as `trave.read` returns them,
back into the items and names `--check` compares (and refuses a script file of another build).

## The format

Each file is a [Trave](https://github.com/edde746/trave#readme) file with the magic `WDTT`. Apps read it with any
Trave library (Python `trave`, Go `github.com/edde746/trave/go`, Kotlin `dev.edde746:trave`, Dart `trave`) or with
nothing but a gzip decoder by following the spec. A reader finds tables and columns by name and skips any it doesn't
need; new columns and tables may appear in later files, and their order may change.

The core, `wikidata-titles.bin.gz`, holds the tables `build`, `items` and `names`; each
`wikidata-names-<script>.bin.gz` holds `build` and its `names_<script>` table.

### `build`

One row, in every file.

| Column | Type | Contents |
|---|---|---|
| `id` | str | the build's id: the SHA-256 (64 hex digits) of the Trave file (unpacked) holding just the core's `items` and `names` tables, in that order (magic, version, 2 tables) |

A script file goes with the core whose `build.id` is its own: its `item` column points at that core's rows. A reader
that loads a script file of another build (say, a core and a script file from different releases) must not use it.
The same data always gives the same id.

### `items`

One row per item, in the order of their Q-numbers.

| Column | Type | Wikidata |
|---|---|---|
| `qid` | int_delta | the item's Q-number (`Q42` is 42) |
| `tmdb_movie` | int_list | TMDB movie IDs (P4947), sorted |
| `tmdb_tv` | int_list | TMDB TV series IDs (P4983), sorted |
| `anidb` | int_list | AniDB anime IDs (P5646), sorted |
| `mal` | int_list | MyAnimeList anime IDs (P4086), sorted |
| `anilist` | int_list | AniList media IDs (P8729), sorted |
| `p31` | int_list | instance of (P31): the classes' Q-numbers, sorted (`11424` film, `5398426` television series, `63952888` anime television series, `220898` OVA, `100269041` anime television series season, ...) |
| `p179` | int_list | part of the series (P179): the series' Q-numbers, sorted |
| `published` | int | the earliest year of its publication dates (P577) |
| `start` | int | the earliest year of its start times (P580) |
| `kind` | dict | `film`, `series`, `season`, `episode` or `other` (below) |
| `end` | int | the latest year of its end times (P582) |
| `episodes` | int | number of episodes (P1113), the largest if several |
| `seasons` | int | number of seasons (P2437), the largest if several |
| `label_langs` | int | the number of languages it has a label in, all languages (before the names are filtered) |
| `anime` | int | `1` when it is anime (below), absent otherwise |
| `follows` | int_list | follows (P155): the items' Q-numbers, sorted |
| `followed_by` | int_list | followed by (P156): the items' Q-numbers, sorted |

A list is absent, not empty, when the item has no such statement. Identifiers are kept when they are decimal
numbers (Wikidata flags the few that aren't); "unknown value" statements are left out. A year is the year as
written in the date (negative for BC), whatever the date's precision. A count is kept when it is a whole number
above zero.

`kind` and `anime` are the only values worked out rather than copied, so that an app needs no table of classes:

- `kind` is `series` for an item with a TMDB TV ID, else `film` for one with a TMDB movie ID; otherwise the first
  of `episode`, `season`, `series` and `film` whose words appear in the English label of one of its classes (P31),
  lowercased: "episode"; "season"; "series", "television program" or "serial"; "film" or "movie". Else `other`.
- `anime` is set for an item with an AniDB, MyAnimeList or AniList ID, or with a class whose English label has the
  word "anime".

The first ten columns are the file's first schema; the others came later, after them (a reader that doesn't
know them skips them).

### `names`

One row per distinct (item, source, language, text), ordered by item, source (in the order below), language and
text.

| Column | Type | Wikidata |
|---|---|---|
| `item` | int_delta | the item's row in `items` (from 0) |
| `source` | dict | `label`, `alias` (skos:altLabel), `P1476` (title), `P1705` (native label) or `P2125` (Revised Hepburn romanisation, as a statement or as a qualifier of P1476 or P1705) |
| `lang` | dict | the text's language code (`en`, `en-gb`, `ja`, `mul`, ...); absent for P2125 values, which have none |
| `text` | str | the text, verbatim |

Labels and aliases are kept in English, Japanese and `mul` and their variants (a code equal to `en`, `ja` or `mul`
or starting with it and `-`); P1476, P1705 and P2125 in every language.

### `names_latin`, `names_cyrillic`, `names_cjk`, `names_arabic`, `names_other`

The labels and aliases in every other language, one table per script, each in its own file
(`wikidata-names-<script>.bin.gz`), with the columns of `names` (in its order); `item` is a row of the same build's
core. An app downloads the files of the scripts its users' names are written in.

- A text goes to the table of the script most of its letters are in, by the first word of their Unicode character
  names, or the second after `FULLWIDTH` or `HALFWIDTH` (`LATIN`; `CYRILLIC`; `CJK`, `HANGUL`, `HIRAGANA`,
  `KATAKANA`, `BOPOMOFO` or `IDEOGRAPHIC`; `ARABIC`), to `names_other` otherwise (Greek, Hebrew, Indic scripts,
  Thai, ..., and text without letters); a tie goes to the earlier table.
- There is one row per distinct text of an item: the label if a label has that text, else the alias, in the
  language whose code sorts first. Many languages share a label, so this keeps one row where Wikidata has dozens.
- A text the item already has in `names` is left out.

| Table (file) | Rows | Bytes (gzip) |
|---|---|---|
| `names_latin` | 687,000 | 8.4 MB |
| `names_cyrillic` | 143,000 | 1.7 MB |
| `names_cjk` | 193,000 | 2.0 MB |
| `names_arabic` | 111,000 | 1.2 MB |
| `names_other` | 108,000 | 1.6 MB |
