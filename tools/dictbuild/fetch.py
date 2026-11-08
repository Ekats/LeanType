#!/usr/bin/env python3
"""
Download and clean corpus text for dictbuild.py.

  fetch.py <lang.json> <work> [source-name ...]

Writes <work>/text/<source>/NNNNN.txt.gz (one paragraph/utterance per line) until the source's
"max_tokens" budget is reached, plus <work>/text/<source>/SOURCE.json describing what was used
(exact URLs, crawl ids, dump dates) so every release can say what it was built from.

Source types (the "type" field in the language config):
  opus        OpenSubtitles monolingual text from OPUS (conversational, closest to what people type)
  hf_parquet  parquet shards from a Hugging Face dataset (FineWeb, FineWeb-2), newest dumps first
  ccwet       plain-text WET files of the newest Common Crawl, filtered by CC's own language tags
  wikipedia   the current Wikipedia pages-articles dump, wikitext stripped to prose
"""
import bz2
import gzip
import hashlib
import io
import json
import os
import random
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

UA = "LeanType-dictbuild/1.0 (+https://github.com/Ekats/LeanType)"
SHARD_TOKENS = 5_000_000


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def http_open(url, retries=5, headers=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    token = os.environ.get("HF_TOKEN")
    if token and "huggingface.co" in url:
        h["Authorization"] = f"Bearer {token}"
    for attempt in range(retries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=120)
        except Exception as e:  # CC and HF occasionally answer 503/429 under load
            if attempt == retries - 1:
                raise
            wait = 10 * (attempt + 1)
            log(f"  retry {attempt + 1} for {url}: {e}; sleeping {wait}s")
            time.sleep(wait)


def http_json(url):
    with http_open(url) as r:
        return json.load(r)


class ShardWriter:
    """Writes cleaned lines into gzip shards and stops at the token budget. Drops exact duplicate lines."""

    def __init__(self, work, name, max_tokens):
        self.dir = os.path.join(work, "text", name)
        os.makedirs(self.dir, exist_ok=True)
        self.max_tokens = max_tokens
        self.tokens = 0
        self.shard_tokens = 0
        self.index = 0
        self.f = None
        self.seen = set()
        self.lines = 0

    @property
    def full(self):
        return self.tokens >= self.max_tokens

    def write(self, line):
        line = line.strip()
        if not line:
            return
        h = hashlib.blake2b(line.encode("utf-8", "replace"), digest_size=8).digest()
        if h in self.seen:
            return
        self.seen.add(h)
        if self.f is None or self.shard_tokens >= SHARD_TOKENS:
            if self.f:
                self.f.close()
            self.f = gzip.open(os.path.join(self.dir, f"{self.index:05d}.txt.gz"), "wt", encoding="utf-8", compresslevel=3)
            self.index += 1
            self.shard_tokens = 0
        self.f.write(line + "\n")
        n = line.count(" ") + 1
        self.tokens += n
        self.shard_tokens += n
        self.lines += 1
        if self.lines % 2_000_000 == 0:
            log(f"  {self.tokens:,} tokens")

    def close(self, meta):
        if self.f:
            self.f.close()
        meta.update({"tokens": self.tokens, "lines": self.lines, "fetched": time.strftime("%Y-%m-%d")})
        with open(os.path.join(self.dir, "SOURCE.json"), "w") as f:
            json.dump(meta, f, indent=2)
        log(f"  done: {self.tokens:,} tokens in {self.index} shards")


# ------------------------------------------------------------------ text quality

LETTER_RE = re.compile(r"[^\W\d_]")
URLISH_RE = re.compile(r"https?://|www\.|\S+@\S+\.\w+|\{|\}|</?\w+>")


def prose_lines(text, min_words=4):
    """Keep lines that look like running text, not menus, code or link lists."""
    for line in text.split("\n"):
        line = line.strip()
        if len(line) < 20:
            continue
        words = line.split()
        if len(words) < min_words or URLISH_RE.search(line):
            continue
        letters = len(LETTER_RE.findall(line))
        if letters < 0.65 * len(line.replace(" ", "")):
            continue
        if line.isupper():
            continue
        if sum(line.count(c) for c in "|•»›·") >= 2:  # navigation menus, breadcrumbs
            continue
        if len(words) < 8 and not line.endswith((".", "!", "?", "…", '"', "”", ")")):
            continue
        yield line


class HttpRangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP range requests (enough for pyarrow's parquet reader)."""

    def __init__(self, url, block=8 << 20):
        self.url, self.block, self.pos = url, block, 0
        with http_open(url, headers={"Range": "bytes=0-0"}) as r:
            rng = r.headers.get("Content-Range", "")
            self.size = int(rng.rsplit("/", 1)[-1]) if "/" in rng else int(r.headers["Content-Length"])
            self.url = r.geturl()  # follow the redirect to the CDN once
        self.cache_start, self.cache = -1, b""

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else (self.pos + offset if whence == 1 else self.size + offset)
        return self.pos

    def _fetch(self, start, end):
        with http_open(self.url, headers={"Range": f"bytes={start}-{end - 1}"}) as r:
            return r.read()

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        cs = self.cache_start
        if not (cs >= 0 and cs <= self.pos and self.pos + n <= cs + len(self.cache)):
            end = min(self.size, self.pos + max(n, self.block))
            self.cache_start, self.cache = self.pos, self._fetch(self.pos, end)
            cs = self.cache_start
        out = self.cache[self.pos - cs:self.pos - cs + n]
        self.pos += len(out)
        return out

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


# ------------------------------------------------------------------ OpenSubtitles (OPUS)

def fetch_opus(src, lang_code, w):
    corpus, version = src.get("corpus", "OpenSubtitles"), src.get("version", "latest")
    api = f"https://opus.nlpl.eu/opusapi/?corpus={corpus}&source={lang_code}&preprocessing=mono&version={version}"
    url = src.get("url")
    meta = {"type": "opus", "corpus": corpus}
    if not url:
        try:
            info = http_json(api)
            cands = [c for c in info.get("corpora", []) if c.get("url", "").endswith((".txt.gz", ".gz"))]
            best = max(cands, key=lambda c: c.get("version", ""))
            url, meta["version"] = best["url"], best.get("version")
        except Exception as e:  # API down or format changed: fall back to the known v2024 layout
            log(f"[opus] API lookup failed ({e}), using v2024 mono URL")
            url, meta["version"] = f"https://object.pouta.csc.fi/OPUS-{corpus}/v2024/mono/{lang_code}.txt.gz", "v2024"
    meta["url"] = url
    log(f"[opus] {url}")
    prev = None
    with http_open(url) as r, gzip.open(r, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip().lstrip("-").strip()
            if not line or line == prev:  # subtitles repeat lines across adjacent frames
                continue
            prev = line
            w.write(line)
            if w.full:
                break
    w.close(meta)


# ------------------------------------------------------------------ Hugging Face parquet (FineWeb / FineWeb-2)

def fetch_hf_parquet(src, w):
    import pyarrow.parquet as pq
    repo, path = src["dataset"], src["path"]
    try:
        if "{lang}" in path:
            raise LookupError(path)
        entries = http_json(f"https://huggingface.co/api/datasets/{repo}/tree/main/{path}")
    except Exception:
        # language folders use GlotLID codes (e.g. Estonian is ekk_Latn, not est_Latn): search for them
        parent, _, rest = path.partition("/{lang}")
        cands = src.get("lang_dirs", [])
        listing = [e["path"] for e in http_json(f"https://huggingface.co/api/datasets/{repo}/tree/main/{parent}") if e["type"] == "directory"]
        match = next((d for c in cands for d in listing if d.rsplit("/", 1)[-1] == c), None)
        if not match:
            raise RuntimeError(f"{path} not found in {repo}; candidates {cands}; available e.g. {listing[:20]}")
        path = match + rest
        log(f"[hf] using {path}")
        entries = http_json(f"https://huggingface.co/api/datasets/{repo}/tree/main/{path}")
    dirs = sorted((e["path"] for e in entries if e["type"] == "directory"), reverse=True)
    if dirs and src.get("newest_dumps"):
        # e.g. FineWeb: data/CC-MAIN-YYYY-WW directories; take a little from each of the newest N dumps
        dirs = [d for d in dirs if re.search(r"CC-MAIN-\d{4}-\d{2}$", d)][: src["newest_dumps"]]
        files = []
        for d in dirs:
            fl = sorted(e["path"] for e in http_json(f"https://huggingface.co/api/datasets/{repo}/tree/main/{d}") if e["path"].endswith(".parquet"))
            files.append(fl[: src.get("files_per_dump", 1)])
        files = [f for group in zip(*files) for f in group] if files else []
    else:
        files = sorted(e["path"] for e in entries if e["path"].endswith(".parquet"))
        if src.get("shuffle", True):
            random.Random(42).shuffle(files)
    meta = {"type": "hf_parquet", "dataset": repo, "files": []}
    per_file_budget = w.max_tokens // max(1, min(len(files), src.get("max_files", 4)))
    for fpath in files[: src.get("max_files", 4)]:
        if w.full:
            break
        url = f"https://huggingface.co/datasets/{repo}/resolve/main/{fpath}"
        log(f"[hf] {url}")
        meta["files"].append(fpath)
        start = w.tokens
        # read only the row groups we need via HTTP range requests instead of downloading GB-sized shards
        pf = pq.ParquetFile(HttpRangeFile(url))
        for rg in range(pf.num_row_groups):
            if w.full or w.tokens - start >= per_file_budget:
                break
            for text in pf.read_row_group(rg, columns=["text"]).column("text").to_pylist():
                for line in prose_lines(text or ""):
                    w.write(line)
    w.close(meta)


# ------------------------------------------------------------------ Common Crawl WET (newest crawl)

def fetch_ccwet(src, w):
    langs = set(src["cc_languages"])  # ISO-639-3 as used in WARC-Identified-Content-Language, e.g. "eng"
    crawls = http_json("https://index.commoncrawl.org/collinfo.json")
    crawl = src.get("crawl") or crawls[0]["id"]  # collinfo lists newest first
    base = "https://data.commoncrawl.org/"
    with http_open(f"{base}crawl-data/{crawl}/wet.paths.gz") as r:
        paths = gzip.decompress(r.read()).decode().split()
    random.Random(src.get("seed", 2026)).shuffle(paths)
    tlds = tuple(src.get("prefer_tlds", []))
    meta = {"type": "ccwet", "crawl": crawl, "files": 0}
    log(f"[ccwet] crawl {crawl}, {len(paths):,} WET files available")
    for p in paths[: src.get("max_files", 40)]:
        if w.full:
            break
        meta["files"] += 1
        try:
            with http_open(base + p) as r, gzip.open(r, "rb") as f:
                for uri, lang, body in _wet_records(f):
                    first = lang.split(",")[0] if lang else ""
                    if first not in langs and not (tlds and _host(uri).endswith(tlds) and first in langs | {""}):
                        continue
                    for line in prose_lines(body):
                        w.write(line)
                    if w.full:
                        break
        except Exception as e:
            log(f"  skipping {p}: {e}")
        log(f"  {meta['files']} WET files, {w.tokens:,} tokens")
    w.close(meta)


def _host(uri):
    m = re.match(r"https?://([^/:]+)", uri or "")
    return m.group(1).lower() if m else ""


def _wet_records(f):
    """Minimal WARC reader for WET files: yields (target_uri, identified_languages, text)."""
    while True:
        line = f.readline()
        if not line:
            return
        if not line.startswith(b"WARC/"):
            continue
        headers = {}
        while True:
            h = f.readline()
            if not h or h in (b"\r\n", b"\n"):
                break
            k, _, v = h.decode("utf-8", "replace").partition(":")
            headers[k.strip().lower()] = v.strip()
        body = f.read(int(headers.get("content-length", 0)))
        if headers.get("warc-type") == "conversion":
            yield headers.get("warc-target-uri", ""), headers.get("warc-identified-content-language", ""), body.decode("utf-8", "replace")


# ------------------------------------------------------------------ Wikipedia (current dump)

WIKI_DROP = [
    (re.compile(r"<!--.*?-->", re.S), ""),
    (re.compile(r"<ref[^>/]*/>"), ""),
    (re.compile(r"<ref[^>]*>.*?</ref>", re.S), ""),
    (re.compile(r"<(math|code|pre|syntaxhighlight|gallery|timeline|score)[^>]*>.*?</\1>", re.S), ""),
    (re.compile(r"^\{\|.*?^\|\}", re.S | re.M), ""),  # tables
]
TEMPLATE_RE = re.compile(r"\{\{[^{}]*\}\}")
FILE_LINK_RE = re.compile(r"\[\[(?:[^\[\]|]*:)[^\[\]]*(?:\[\[[^\]]*\]\][^\[\]]*)*\]\]")  # [[File:...]], [[Category:...]]
LINK_RE = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]")
EXT_LINK_RE = re.compile(r"\[https?://\S+\s*([^\]]*)\]")
TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def wikitext_to_prose(t):
    for rx, rep in WIKI_DROP:
        t = rx.sub(rep, t)
    for _ in range(6):  # nested templates, innermost first
        t, n = TEMPLATE_RE.subn("", t)
        if not n:
            break
    t = FILE_LINK_RE.sub("", t)
    t = LINK_RE.sub(r"\1", t)
    t = EXT_LINK_RE.sub(r"\1", t)
    t = TAG_RE.sub("", t)
    t = t.replace("'''", "").replace("''", "").replace("&nbsp;", " ")
    out = []
    for line in t.split("\n"):
        line = line.strip()
        if not line or line.startswith(("=", "|", "!", "{", "}", "__")):
            continue
        out.append(line.lstrip("*#:; ").strip())
    return "\n".join(out)


def fetch_wikipedia(src, w):
    wiki = src["wiki"]  # e.g. "etwiki"
    url = f"https://dumps.wikimedia.org/{wiki}/latest/{wiki}-latest-pages-articles.xml.bz2"
    meta = {"type": "wikipedia", "url": url}
    with http_open(url, headers=None) as r:
        meta["last_modified"] = r.headers.get("Last-Modified")
        log(f"[wikipedia] {url} ({meta['last_modified']})")
        stream = bz2.BZ2File(r)
        ns_ok = text = None
        for event, el in ET.iterparse(stream, events=("end",)):
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "ns":
                ns_ok = el.text == "0"
            elif tag == "text":
                text = el.text or ""
            elif tag == "page":
                if ns_ok and text and not text.lstrip().lower().startswith("#"):  # skip redirects
                    for line in prose_lines(wikitext_to_prose(text), min_words=5):
                        w.write(line)
                el.clear()
                if w.full:
                    break
    w.close(meta)


def main():
    cfg_path, work = sys.argv[1], sys.argv[2]
    only = set(sys.argv[3:])
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    for src in cfg["sources"]:
        if only and src["name"] not in only:
            continue
        w = ShardWriter(work, src["name"], int(src.get("max_tokens", 100_000_000)))
        t = src["type"]
        log(f"== {src['name']} ({t}), budget {w.max_tokens:,} tokens")
        if t == "opus":
            fetch_opus(src, src.get("lang", cfg["opus_lang"]), w)
        elif t == "hf_parquet":
            fetch_hf_parquet(src, w)
        elif t == "ccwet":
            fetch_ccwet(src, w)
        elif t == "wikipedia":
            fetch_wikipedia(src, w)
        else:
            raise ValueError(t)


if __name__ == "__main__":
    main()
