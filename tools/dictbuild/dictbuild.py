#!/usr/bin/env python3
"""
Build AOSP/LeanType main dictionaries (unigrams + next-word bigrams) from text corpora.

Pipeline (each step is a sub-command so CI can run them as separate stages):

  count   <lang.json> <work>   tokenize every corpus shard in <work>/text/<source>/*.txt.gz
                               -> <work>/counts/<source>.uni.pkl.gz      (pass 1: word counts)
  vocab   <lang.json> <work>   merge per-source counts with source weights, resolve
                               capitalisation, spell-filter, pick the vocabulary
                               -> <work>/vocab.pkl.gz
  bigrams <lang.json> <work>   pass 2: count adjacent word pairs inside vocabulary
                               -> <work>/counts/<source>.bi.npz
  emit    <lang.json> <work> <out.combined>
                               write the "combined" word list consumed by dicttool makedict

Design notes
- Bigrams never span punctuation: the keyboard drops the previous-word context after a comma,
  quote, bracket etc. (NgramContextUtils), so such pairs would never be looked up.
- Main dictionaries must be binary format v202 (the only single-file format the native code
  accepts), which stores one previous word of context. A bigram's probability is stored as one
  of 16 steps between the target word's unigram probability and 255, so we hand dicttool the
  *absolute* contextual probability we want and let it discretise.
- Probabilities use the scale of the 2014 AOSP dictionaries (the=222, keyboard=118,
  serendipity=56): f = 255 + 24 * log10(p). Autocorrect thresholds were tuned against that scale.
"""
import gzip
import json
import math
import os
import pickle
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from multiprocessing import Pool

import numpy as np

# A word: letters (any script), optionally joined by apostrophes or hyphens (don't, e-mail, Wi-Fi).
WORD_RE = re.compile(r"[^\W\d_]+(?:['\-][^\W\d_]+)*")
# Runs of words separated only by whitespace form one "segment"; anything else breaks context.
SEGMENT_SPLIT_RE = re.compile(r"[^\w\s'\-]+|(?<!\w)['\-]|['\-](?!\w)|\d+|_+")
SENTENCE_END_RE = re.compile(r"[.!?…]")
APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "´": "'", "`": "'", "‐": "-", "‑": "-"})
MAX_WORD_LEN = 48


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def open_text(path):
    return gzip.open(path, "rt", encoding="utf-8", errors="replace") if path.endswith(".gz") else open(path, encoding="utf-8", errors="replace")


def segments(line):
    """Yield (words, starts_sentence) for each punctuation-free run of words in a line."""
    line = line.translate(APOSTROPHES)
    sentence_start = True
    pos = 0
    for m in SEGMENT_SPLIT_RE.finditer(line):
        chunk = line[pos:m.start()]
        words = WORD_RE.findall(chunk)
        if words:
            yield words, sentence_start
            sentence_start = False
        if SENTENCE_END_RE.search(m.group()):
            sentence_start = True
        pos = m.end()
    words = WORD_RE.findall(line[pos:])
    if words:
        yield words, sentence_start


def shard_files(work, source):
    d = os.path.join(work, "text", source)
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith((".txt", ".txt.gz")))


# ---------------------------------------------------------------- pass 1: unigrams

def _count_unigrams(path):
    total, initial = Counter(), Counter()
    tokens = 0
    with open_text(path) as f:
        for line in f:
            for words, starts in segments(line):
                words = [w for w in words if len(w) <= MAX_WORD_LEN]
                if not words:
                    continue
                total.update(words)
                if starts:
                    initial[words[0]] += 1
                tokens += len(words)
    return total, initial, tokens


def cmd_count(cfg, work, jobs):
    os.makedirs(os.path.join(work, "counts"), exist_ok=True)
    for source in cfg["sources"]:
        name = source["name"]
        files = shard_files(work, name)
        if not files:
            log(f"[count] {name}: no text shards, skipping")
            continue
        total, initial, tokens = Counter(), Counter(), 0
        with Pool(jobs) as pool:
            for i, (t, ini, n) in enumerate(pool.imap_unordered(_count_unigrams, files)):
                total.update(t)
                initial.update(ini)
                tokens += n
                if len(total) > 30_000_000:  # lossy pruning keeps memory bounded on huge corpora
                    total = Counter({k: v for k, v in total.items() if v > 1})
                    initial = Counter({k: v for k, v in initial.items() if k in total})
                log(f"[count] {name}: {i + 1}/{len(files)} shards, {tokens:,} tokens, {len(total):,} forms")
        with gzip.open(os.path.join(work, "counts", f"{name}.uni.pkl.gz"), "wb") as f:
            pickle.dump({"total": total, "initial": initial, "tokens": tokens}, f, protocol=5)


# ---------------------------------------------------------------- vocabulary

def source_weights(cfg, work):
    """Scale each source so it contributes its configured share of the probability mass."""
    stats = {}
    for source in cfg["sources"]:
        p = os.path.join(work, "counts", f"{source['name']}.uni.pkl.gz")
        if os.path.exists(p):
            with gzip.open(p, "rb") as f:
                stats[source["name"]] = pickle.load(f)
    shares = {s["name"]: s.get("share", 1.0) for s in cfg["sources"] if s["name"] in stats}
    norm = sum(shares.values())
    total_tokens = sum(st["tokens"] for st in stats.values())
    weights = {n: (shares[n] / norm) * total_tokens / max(1, stats[n]["tokens"]) for n in stats}
    return stats, weights


class SpellChecker:
    """Accepts words known to Hunspell. Uses the hunspell binary when present (fast), else spylls."""

    def __init__(self, dic_paths):
        self.dic_paths = [p for p in dic_paths if os.path.exists(p + ".dic")]
        self.binary = subprocess.run(["which", "hunspell"], capture_output=True).returncode == 0

    def known(self, words):
        if not self.dic_paths:
            return set(words)
        words = list(words)
        accepted = set()
        for dic in self.dic_paths:
            todo = [w for w in words if w not in accepted]
            if self.binary:
                p = subprocess.run(["hunspell", "-d", dic, "-l", "-i", "utf-8"], input="\n".join(todo),
                                   capture_output=True, text=True)
                bad = set(p.stdout.split("\n"))
                accepted.update(w for w in todo if w not in bad)
            else:
                from spylls.hunspell import Dictionary
                d = Dictionary.from_files(dic)
                accepted.update(w for w in todo if d.lookup(w))
        return accepted


def resolve_case(total, initial):
    """
    Group raw forms by lowercase and decide which capitalisations to emit.
    Sentence-initial occurrences say nothing about natural case, so the decision uses
    non-initial occurrences; the full count is then split between emitted variants.
    Returns {emitted_form: count}.
    """
    groups = defaultdict(list)
    for form, c in total.items():
        groups[form.lower()].append(form)
    out = {}
    for low, forms in groups.items():
        grand = sum(total[f] for f in forms)
        non_initial = {f: total[f] - initial.get(f, 0) for f in forms}
        ni_sum = sum(non_initial.values())
        if ni_sum <= 0:
            # only ever seen at sentence start: prefer lowercase if it exists at all, else the most common form
            chosen = {low: 1.0} if low in total else {max(forms, key=lambda f: total[f]): 1.0}
        else:
            shares = {f: v / ni_sum for f, v in non_initial.items() if v > 0}
            chosen = {f: s for f, s in shares.items() if s >= 0.2}
            if not chosen:
                best = max(shares, key=shares.get)
                chosen = {best: 1.0}
        norm = sum(chosen.values())
        for f, s in chosen.items():
            out[f] = out.get(f, 0.0) + grand * s / norm
    return out


def prob_to_f(p):
    return 255.0 + 24.0 * math.log10(p) if p > 0 else 0.0


def cmd_vocab(cfg, work):
    stats, weights = source_weights(cfg, work)
    log("[vocab] source weights:", {k: round(v, 3) for k, v in weights.items()})
    per_source_resolved = {}
    merged_total, merged_initial = Counter(), Counter()
    for name, st in stats.items():
        w = weights[name]
        for k, v in st["total"].items():
            merged_total[k] += v * w
        for k, v in st["initial"].items():
            merged_initial[k] += v * w
        per_source_resolved[name] = set(f.lower() for f, c in st["total"].items() if c >= cfg.get("min_count_per_source", 3))
    resolved = resolve_case(merged_total, merged_initial)
    total_mass = sum(resolved.values())
    v = cfg["vocab"]
    min_p = v.get("min_probability", 1e-8)
    candidates = {f: c for f, c in resolved.items() if c / total_mass >= min_p}
    log(f"[vocab] {len(resolved):,} resolved forms, {len(candidates):,} above p>={min_p}")

    # spell filter: Hunspell-known words always pass; unknown words need to be common in several sources
    spell = SpellChecker([os.path.join(work, "hunspell", d) for d in cfg.get("hunspell", [])])
    known = spell.known(candidates.keys())
    known_lower = spell.known({f.lower() for f in candidates if f not in known} | set())
    unknown_min_p = v.get("unknown_word_min_probability", 2e-7)
    unknown_min_sources = v.get("unknown_word_min_sources", 2)
    kept = {}
    for f, c in candidates.items():
        if f in known or f.lower() in known_lower:
            kept[f] = c
            continue
        if len(f) <= 2:  # fragments like "th", "nd" from split tokens: only trust Hunspell for these
            continue
        n_sources = sum(1 for s in per_source_resolved.values() if f.lower() in s)
        if c / total_mass >= unknown_min_p and n_sources >= min(unknown_min_sources, len(per_source_resolved)):
            kept[f] = c
    for w in cfg.get("always_include", []):
        kept.setdefault(w, max(kept.get(w, 0.0), total_mass * min_p))
    for w in cfg.get("exclude", []):
        kept.pop(w, None)
    words = sorted(kept, key=kept.get, reverse=True)[: v.get("max_words", 300_000)]
    log(f"[vocab] kept {len(words):,} words ({sum(1 for w in words if w not in known):,} not in Hunspell)")
    with gzip.open(os.path.join(work, "vocab.pkl.gz"), "wb") as f:
        pickle.dump({"words": words, "counts": {w: kept[w] for w in words}, "total_mass": total_mass,
                     "weights": weights}, f, protocol=5)


# ---------------------------------------------------------------- pass 2: bigrams

_VOCAB_INDEX = None  # raw form -> word id (inherited by forked workers)


def _build_index(words):
    exact = {w: i for i, w in enumerate(words)}
    lower = {}
    for i, w in enumerate(words):
        lower.setdefault(w.lower(), i)  # words are sorted by frequency, so the most common variant wins
    return exact, lower


def _lookup(word, initial):
    exact, lower = _VOCAB_INDEX
    i = exact.get(word)
    if i is not None:
        return i
    # sentence-initial or ALL-CAPS variants map to the emitted form ("The" -> "the", "THANKS" -> "thanks")
    if initial or word.isupper() or word[:1].isupper():
        return lower.get(word.lower(), -1)
    return -1


def _count_bigrams(args):
    path, n_words = args
    keys = []
    buf = []
    with open_text(path) as f:
        for line in f:
            for words, starts in segments(line):
                prev = -1
                for j, w in enumerate(words):
                    i = _lookup(w, starts and j == 0) if len(w) <= MAX_WORD_LEN else -1
                    if prev >= 0 and i >= 0:
                        buf.append(prev * n_words + i)
                    prev = i
            if len(buf) > 2_000_000:
                keys.append(np.unique(np.array(buf, dtype=np.int64), return_counts=True))
                buf = []
    if buf:
        keys.append(np.unique(np.array(buf, dtype=np.int64), return_counts=True))
    return _merge(keys)


def _merge(parts, prune_below=0):
    if not parts:
        return np.zeros(0, np.int64), np.zeros(0, np.float64)
    k = np.concatenate([p[0] for p in parts])
    c = np.concatenate([p[1].astype(np.float64) for p in parts])
    uk, inv = np.unique(k, return_inverse=True)
    uc = np.bincount(inv, weights=c)
    if prune_below:
        m = uc >= prune_below
        uk, uc = uk[m], uc[m]
    return uk, uc


def cmd_bigrams(cfg, work, jobs):
    global _VOCAB_INDEX
    with gzip.open(os.path.join(work, "vocab.pkl.gz"), "rb") as f:
        vocab = pickle.load(f)
    words = vocab["words"]
    _VOCAB_INDEX = _build_index(words)
    n = len(words)
    for source in cfg["sources"]:
        name = source["name"]
        files = shard_files(work, name)
        if not files:
            continue
        acc = []
        with Pool(jobs) as pool:
            for i, part in enumerate(pool.imap_unordered(_count_bigrams, [(p, n) for p in files])):
                acc.append(part)
                if len(acc) >= 8:
                    acc = [_merge(acc)]
                    if len(acc[0][0]) > 150_000_000:
                        acc = [_merge(acc, prune_below=2)]
                log(f"[bigrams] {name}: {i + 1}/{len(files)} shards, {len(acc[0][0]) if len(acc) == 1 else '…'} pairs")
        k, c = _merge(acc)
        np.savez(os.path.join(work, "counts", f"{name}.bi.npz"), keys=k, counts=c)
        log(f"[bigrams] {name}: {len(k):,} distinct pairs")


# ---------------------------------------------------------------- emit combined word list

def cmd_emit(cfg, work, out_path):
    with gzip.open(os.path.join(work, "vocab.pkl.gz"), "rb") as f:
        vocab = pickle.load(f)
    words, counts, total_mass, weights = vocab["words"], vocab["counts"], vocab["total_mass"], vocab["weights"]
    n = len(words)
    parts = []
    for source in cfg["sources"]:
        p = os.path.join(work, "counts", f"{source['name']}.bi.npz")
        if os.path.exists(p) and source["name"] in weights:
            z = np.load(p)
            parts.append((z["keys"], z["counts"] * weights[source["name"]]))
    keys, bcounts = _merge(parts)
    b = cfg["bigrams"]
    w1 = keys // n
    w2 = keys % n
    # context counts from the bigram table itself (how often w1 is followed by any in-vocabulary word)
    ctx = np.bincount(w1, weights=bcounts, minlength=n)
    cond = bcounts / np.maximum(ctx[w1], 1e-9)
    keep = (bcounts >= b.get("min_count", 3)) & (cond >= b.get("min_conditional_probability", 0.002))
    w1, w2, bcounts, cond = w1[keep], w2[keep], bcounts[keep], cond[keep]
    # top-K per context word, K growing with how much evidence we have for that context
    order = np.lexsort((-cond, w1))
    w1, w2, bcounts, cond = w1[order], w2[order], bcounts[order], cond[order]
    starts = np.r_[0, np.flatnonzero(np.diff(w1)) + 1]
    rank = np.arange(len(w1)) - np.repeat(starts, np.diff(np.r_[starts, len(w1)]))
    k_max, k_min = b.get("max_per_word", 24), b.get("min_per_word", 4)
    ctx_k = np.clip(np.round(np.log2(np.maximum(ctx, 1)) * 2), k_min, k_max).astype(np.int64)
    sel = rank < ctx_k[w1]
    w1, w2, cond = w1[sel], w2[sel], cond[sel]
    log(f"[emit] {len(w1):,} bigrams for {len(np.unique(w1)):,} context words")
    bigrams = defaultdict(list)
    for a, c_, p in zip(w1.tolist(), w2.tolist(), cond.tolist()):
        bigrams[a].append((c_, p))

    cfg_dir = cfg.get("_dir", ".")
    offensive = {w.lower() for w in cfg.get("offensive", [])}
    if cfg.get("offensive_file"):
        with open(os.path.join(cfg_dir, cfg["offensive_file"]), encoding="utf-8") as f:
            offensive |= {l.strip().lower() for l in f if l.strip() and not l.startswith("#")}
    # curated entries (whitelisted typo shortcuts, not-a-word entries) in combined format
    extra_shortcuts, extra_entries = defaultdict(list), []
    if cfg.get("extra_file"):
        cur = None
        with open(os.path.join(cfg_dir, cfg["extra_file"]), encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith(" word="):
                    cur = [line]
                    extra_entries.append(cur)
                elif line.startswith("  ") and cur is not None:
                    cur.append(line)
    vocab_set = set(words)
    for e in list(extra_entries):
        w = e[0][len(" word="):].split(",")[0]
        if w in vocab_set:  # real word with a whitelist shortcut: keep our frequency, add the shortcut
            extra_shortcuts[w] += [l for l in e[1:] if l.startswith("  shortcut=")]
            extra_entries.remove(e)

    h = cfg["header"]
    date = int(os.environ.get("DICT_DATE", time.time()))
    version = os.environ.get("DICT_VERSION", time.strftime("%Y%m%d", time.gmtime(date)))
    with open(out_path, "w", encoding="utf-8") as out:
        out.write(f"dictionary=main:{h['id']},locale={h['locale']},description={h['description']},"
                  f"date={date},version={version}\n")
        min_f, max_f = cfg.get("min_f", 20), cfg.get("max_f", 240)
        for i, w in enumerate(words):
            p = counts[w] / total_mass
            f_uni = int(round(min(max_f, max(min_f, prob_to_f(p)))))
            if w.lower() in offensive:
                # like AOSP: keep the real frequency and flag it, the keyboard hides it unless the user opts in
                out.write(f" word={w},f={f_uni},possibly_offensive=true\n")
            else:
                out.write(f" word={w},f={f_uni}\n")
            for sc in extra_shortcuts.get(w, ()):
                out.write(sc + "\n")
            for j, pc in bigrams.get(i, ()):
                if words[j].lower() in offensive:
                    continue
                # absolute probability of the next word in this context; dicttool stores it as one of
                # 16 steps above the target's unigram value, so anything below that is clamped
                f_bi = int(round(min(254, max(1, prob_to_f(pc)))))
                out.write(f"  bigram={words[j]},f={f_bi}\n")
        for e in extra_entries:
            out.write("\n".join(e) + "\n")
    log(f"[emit] wrote {out_path}")


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    cmd, cfg_path, work = sys.argv[1], sys.argv[2], sys.argv[3]
    cfg = load_config(cfg_path)
    cfg["_dir"] = os.path.dirname(os.path.abspath(cfg_path))
    jobs = int(os.environ.get("JOBS", os.cpu_count() or 2))
    if cmd == "count":
        cmd_count(cfg, work, jobs)
    elif cmd == "vocab":
        cmd_vocab(cfg, work)
    elif cmd == "bigrams":
        cmd_bigrams(cfg, work, jobs)
    elif cmd == "emit":
        cmd_emit(cfg, work, sys.argv[4])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
