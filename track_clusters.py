#!/usr/bin/env python3
"""Daily JustBid CA liquidation cluster finder & incremental tracker.

Maintains a local SQLite index of active CA JustBid listings:
- First run: backfills the active CA catalog (sorted by newly_posted).
- Subsequent runs: incrementally scans newly_posted pages until overlapping
  with already-indexed listings, then drops expired listings locally using
  each listing's end_timestamp.
- Downloads each listing's 300x300 reference thumbnail ONCE (in memory, no
  image files stored on disk) to compute 64-bit perceptual hashes (pHash + dHash).
- Clusters active items by perceptual image similarity (default, <1s for 43k+ items)
  or fast prefix-filtered TF-IDF text similarity, diffs against cluster history
  to highlight NEW and GROWING clusters, and writes daily HTML + JSON reports
  with `latest.html` / `latest.json` symlinks.

Usage:
  python3 track_clusters.py
  python3 track_clusters.py --no-fetch            # hash any missing thumbnails & re-cluster
  python3 track_clusters.py --image-hamming 12    # looser visual match (groups sibling variants)
  python3 track_clusters.py --cluster-by text     # use fast 4s TF-IDF text clustering instead
  python3 track_clusters.py --full-sync           # force full CA re-scan
  python3 track_clusters.py --dry-run             # don't update cluster history
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import io
import json
import re
import sqlite3
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests
from PIL import Image
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.fftpack import dct
from scipy.sparse import csr_matrix

from fetch_catalog import DEFAULT_USER_AGENT, JustBidClient

# Precomputed popcount lookup table for 16-bit integers (fast vectorized Hamming distance)
_POPCOUNT16 = np.array([bin(i).count("1") for i in range(65536)], dtype=np.uint8)


def hamming64_vec(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Vectorized Hamming distance between uint64 arrays a and b."""
    x = np.bitwise_xor(a, b)
    return (
        _POPCOUNT16[x & np.uint64(0xFFFF)].astype(np.uint8)
        + _POPCOUNT16[(x >> np.uint64(16)) & np.uint64(0xFFFF)]
        + _POPCOUNT16[(x >> np.uint64(32)) & np.uint64(0xFFFF)]
        + _POPCOUNT16[(x >> np.uint64(48)) & np.uint64(0xFFFF)]
    )


def compute_image_hashes(img_bytes: bytes) -> tuple[str, str, str]:
    """Compute (sha256_hex, dhash_hex_16, phash_hex_16) from image bytes."""
    sha = hashlib.sha256(img_bytes).hexdigest()
    im = Image.open(io.BytesIO(img_bytes)).convert("L")

    # 64-bit dHash (9x8 horizontal gradient)
    g9 = np.asarray(im.resize((9, 8), Image.Resampling.BILINEAR), dtype=np.float32)
    dbits = (g9[:, 1:] > g9[:, :-1]).flatten()
    dval = 0
    for bit in dbits:
        dval = (dval << 1) | int(bit)

    # 64-bit pHash (32x32 2D DCT -> top-left 8x8 low-frequency block)
    g32 = np.asarray(im.resize((32, 32), Image.Resampling.BILINEAR), dtype=np.float32)
    d = dct(dct(g32.T, norm="ortho").T, norm="ortho")
    low = d[:8, :8].flatten()
    med = float(np.median(low[1:]))
    pbits = low > med
    pval = 0
    for bit in pbits:
        pval = (pval << 1) | int(bit)

    return sha, f"{dval:016x}", f"{pval:016x}"


def normalize_title(title: str | None) -> str:
    t = (title or "").lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_similarity(norm_a: str, norm_b: str) -> float:
    """Fast token Jaccard similarity for cross-day cluster matching."""
    if norm_a == norm_b:
        return 1.0
    toks_a = set(norm_a.split())
    toks_b = set(norm_b.split())
    if not toks_a or not toks_b:
        return 0.0
    return len(toks_a & toks_b) / len(toks_a | toks_b)


def assemble_clusters(
    member_groups: list[list[dict[str, Any]]],
    *,
    min_cluster_size: int = 3,
) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for cluster_members in member_groups:
        if len(cluster_members) < min_cluster_size:
            continue

        cluster_members.sort(
            key=lambda x: (
                x.get("current_bid") if x.get("current_bid") is not None else 1e9,
                x.get("end_timestamp") or 1e12,
            )
        )

        title_counts = Counter(x.get("title") or "" for x in cluster_members)
        rep_title = title_counts.most_common(1)[0][0]
        rep_norm = normalize_title(rep_title)

        retails = [
            x["retail_price"]
            for x in cluster_members
            if x.get("retail_price") is not None
        ]
        bids = [
            x["current_bid"]
            for x in cluster_members
            if x.get("current_bid") is not None
        ]
        ends = [
            x["ends_at"] for x in cluster_members if x.get("ends_at") is not None
        ]
        loc_counts = Counter(
            x.get("location") or "Unknown" for x in cluster_members
        )
        thumb = next(
            (x["image_url"] for x in cluster_members if x.get("image_url")), None
        )
        phash = next(
            (x.get("img_phash") for x in cluster_members if x.get("img_phash")), None
        )

        clusters.append(
            {
                "canonical_title": rep_title,
                "canonical_norm": rep_norm,
                "canonical_phash": phash,
                "count": len(cluster_members),
                "distinct_titles": [
                    {"title": t, "count": c} for t, c in title_counts.most_common()
                ],
                "min_retail": min(retails) if retails else None,
                "median_retail": round(statistics.median(retails), 2)
                if retails
                else None,
                "max_retail": max(retails) if retails else None,
                "min_current_bid": min(bids) if bids else None,
                "median_current_bid": round(statistics.median(bids), 2)
                if bids
                else None,
                "locations": dict(loc_counts.most_common()),
                "earliest_end": min(ends) if ends else None,
                "latest_end": max(ends) if ends else None,
                "thumbnail_url": thumb,
                "item_ids": [
                    str(x["item_id"]) for x in cluster_members if x.get("item_id")
                ],
                "items": cluster_members,
            }
        )

    clusters.sort(
        key=lambda c: (
            -c["count"],
            -(c["median_retail"] or 0),
        )
    )
    return clusters


def _group_by_image_raw(
    items: list[dict[str, Any]],
    *,
    max_hamming: int = 6,
) -> list[list[dict[str, Any]]]:
    """Pass 1: group items by perceptual image hash (pHash + dHash).

    Returns raw member lists (including size 1/2 groups) so a second pass can
    merge them by text before applying min_cluster_size.
    """
    exact_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    unhashed: list[dict[str, Any]] = []
    for it in items:
        ph = it.get("img_phash")
        dh = it.get("img_dhash")
        if ph and dh:
            exact_groups[(ph, dh)].append(it)
        else:
            unhashed.append(it)

    if not exact_groups:
        return [[it] for it in unhashed]

    keys = list(exact_groups.keys())
    M = len(keys)
    if max_hamming <= 0 or M <= 1:
        return list(exact_groups.values()) + [[it] for it in unhashed]

    phashes = np.array([int(k[0], 16) for k in keys], dtype=np.uint64)
    dhashes = np.array([int(k[1], 16) for k in keys], dtype=np.uint64)

    n_bands = 8
    band_bits = 8
    mask = np.uint64((1 << band_bits) - 1)
    max_bucket_size = 500

    band_tables: list[dict[int, list[int]]] = [defaultdict(list) for _ in range(n_bands)]
    for b in range(n_bands):
        shift = np.uint64(b * band_bits)
        vals = ((phashes >> shift) & mask).tolist()
        tbl = band_tables[b]
        for idx, v in enumerate(vals):
            tbl[v].append(idx)

    dhash_limit = max_hamming + 2
    adj: dict[int, list[int]] = defaultdict(list)
    seen_pairs: set[int] = set()

    for tbl in band_tables:
        for bucket in tbl.values():
            blen = len(bucket)
            if blen < 2 or blen > max_bucket_size:
                continue
            arr = np.array(bucket, dtype=np.int32)
            iu, ju = np.triu_indices(blen, k=1)
            gi = arr[iu]
            gj = arr[ju]
            pd = hamming64_vec(phashes[gi], phashes[gj])
            ok = pd <= max_hamming
            if not np.any(ok):
                continue
            gi = gi[ok]
            gj = gj[ok]
            dd = hamming64_vec(dhashes[gi], dhashes[gj])
            ok2 = dd <= dhash_limit
            for a, b_idx in zip(gi[ok2].tolist(), gj[ok2].tolist()):
                lo, hi = (a, b_idx) if a < b_idx else (b_idx, a)
                pair_key = lo * M + hi
                if pair_key not in seen_pairs:
                    seen_pairs.add(pair_key)
                    adj[lo].append(hi)
                    adj[hi].append(lo)

    visited: set[int] = set()
    member_groups: list[list[dict[str, Any]]] = []

    for i in range(M):
        if i in visited:
            continue
        if i not in adj:
            visited.add(i)
            member_groups.append(exact_groups[keys[i]])
            continue

        comp: list[int] = []
        stack = [i]
        visited.add(i)
        while stack:
            u = stack.pop()
            comp.append(u)
            for nb in adj[u]:
                if nb not in visited:
                    visited.add(nb)
                    stack.append(nb)

        k = len(comp)
        if k == 2:
            merged = exact_groups[keys[comp[0]]] + exact_groups[keys[comp[1]]]
            member_groups.append(merged)
            continue

        c_arr = np.array(comp, dtype=np.int32)
        iu, ju = np.triu_indices(k, k=1)
        pd = hamming64_vec(phashes[c_arr[iu]], phashes[c_arr[ju]]).astype(np.float64)
        dd = hamming64_vec(dhashes[c_arr[iu]], dhashes[c_arr[ju]]).astype(np.float64)
        dists = 0.6 * pd + 0.4 * dd
        Z = linkage(dists, method="average")
        labels = fcluster(Z, t=float(max_hamming) + 0.5, criterion="distance")
        by_lbl: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for idx, lbl in zip(comp, labels):
            by_lbl[int(lbl)].extend(exact_groups[keys[idx]])
        member_groups.extend(by_lbl.values())

    for it in unhashed:
        member_groups.append([it])
    return member_groups


def cluster_items_by_image(
    items: list[dict[str, Any]],
    *,
    min_cluster_size: int = 3,
    max_hamming: int = 6,
) -> list[dict[str, Any]]:
    """Sub-second exact + near-duplicate perceptual image clustering."""
    member_groups = _group_by_image_raw(items, max_hamming=max_hamming)
    return assemble_clusters(member_groups, min_cluster_size=min_cluster_size)


def _merge_groups_by_text(
    input_groups: list[list[dict[str, Any]]],
    *,
    similarity_threshold: float = 0.78,
    max_price_ratio: float = 1.75,
) -> list[list[dict[str, Any]]]:
    """Merge a list of item groups whose canonical titles are textually similar.

    Uses exact normalized-title grouping followed by Cauchy-Schwarz prefix-filtered
    Word TF-IDF cosine + average-linkage clustering.
    """
    if not input_groups:
        return []

    # Represent each input group by its most common normalized title & median retail price
    norm_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    untitled_groups: list[list[dict[str, Any]]] = []
    for grp in input_groups:
        norm_counts = Counter(
            nt for it in grp if (nt := normalize_title(it.get("title")))
        )
        if not norm_counts:
            untitled_groups.append(grp)
            continue
        rep_norm = norm_counts.most_common(1)[0][0]
        norm_groups[rep_norm].extend(grp)

    unique_norms = list(norm_groups.keys())
    M = len(unique_norms)
    if M <= 1:
        return list(norm_groups.values()) + untitled_groups

    group_median_price: list[float] = []
    for nt in unique_norms:
        prices = [
            x["retail_price"]
            for x in norm_groups[nt]
            if x.get("retail_price") and x["retail_price"] > 0
        ]
        group_median_price.append(statistics.median(prices) if prices else 0.0)

    tokenized = [nt.split() for nt in unique_norms]
    doc_freq: Counter[str] = Counter()
    for toks in tokenized:
        for tok in set(toks):
            doc_freq[tok] += 1

    vocab: dict[str, int] = {}
    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    for i, toks in enumerate(tokenized):
        tf = Counter(toks)
        for tok, cnt in tf.items():
            idx = vocab.get(tok)
            if idx is None:
                idx = len(vocab)
                vocab[tok] = idx
            rows.append(i)
            cols.append(idx)
            idf = np.log((M + 1) / (doc_freq[tok] + 1)) + 1.0
            vals.append((1.0 + np.log(cnt)) * idf)

    X = csr_matrix((vals, (rows, cols)), shape=(M, len(vocab)), dtype=np.float32)
    norms = np.sqrt(X.multiply(X).sum(axis=1)).A1
    norms[norms == 0] = 1.0
    X = X.multiply(1.0 / norms[:, None]).tocsr()

    cand_thresh = min(0.66, similarity_threshold - 0.10)
    tail_max = cand_thresh * 0.65
    need_sq = 1.0 - (tail_max * tail_max)

    indptr, indices, data = X.indptr, X.indices, X.data
    p_rows: list[int] = []
    p_cols: list[int] = []
    p_vals: list[float] = []
    row_tail_norm = np.zeros(M, dtype=np.float32)

    for i in range(M):
        s, e = indptr[i], indptr[i + 1]
        if s == e:
            continue
        r_idx = indices[s:e]
        r_val = data[s:e]
        order = np.argsort(r_val)[::-1]
        r_idx = r_idx[order]
        r_val = r_val[order]
        csum = np.cumsum(r_val * r_val)
        cut = min(int(np.searchsorted(csum, need_sq)) + 1, len(r_val))
        p_rows.extend([i] * cut)
        p_cols.extend(r_idx[:cut].tolist())
        p_vals.extend(r_val[:cut].tolist())
        row_tail_norm[i] = np.sqrt(max(0.0, 1.0 - float(csum[cut - 1])))

    X_pref = csr_matrix((p_vals, (p_rows, p_cols)), shape=(M, len(vocab)), dtype=np.float32)
    XT = X.T.tocsr()
    prices_arr = np.array(group_median_price, dtype=np.float64)
    adj: dict[int, list[int]] = defaultdict(list)

    cand_gi, cand_gj = [], []
    for start in range(0, M, 2000):
        end = min(start + 2000, M)
        blk = (X_pref[start:end] @ XT).tocoo()
        gi = blk.row + start
        gj = blk.col
        mask = (gi < gj) & ((blk.data + row_tail_norm[gi]) >= (cand_thresh - 1e-5))
        if not np.any(mask):
            continue
        gi = gi[mask]
        gj = gj[mask]
        p1, p2 = prices_arr[gi], prices_arr[gj]
        both_pos = (p1 > 0) & (p2 > 0)
        ok = np.ones(len(gi), dtype=bool)
        if np.any(both_pos):
            ok[both_pos] = (
                np.maximum(p1[both_pos], p2[both_pos])
                / np.minimum(p1[both_pos], p2[both_pos])
            ) <= max_price_ratio
        if np.any(ok):
            cand_gi.append(gi[ok])
            cand_gj.append(gj[ok])

    if cand_gi:
        all_i = np.concatenate(cand_gi)
        all_j = np.concatenate(cand_gj)
        exact_dots = np.asarray(X[all_i].multiply(X[all_j]).sum(axis=1)).ravel()
        keep = exact_dots >= cand_thresh
        for i, j in zip(all_i[keep].tolist(), all_j[keep].tolist()):
            adj[i].append(j)
            adj[j].append(i)

    visited: set[int] = set()
    member_groups: list[list[dict[str, Any]]] = []

    for i in range(M):
        if i in visited:
            continue
        if i not in adj:
            visited.add(i)
            member_groups.append(norm_groups[unique_norms[i]])
            continue

        comp: list[int] = []
        stack = [i]
        visited.add(i)
        while stack:
            u = stack.pop()
            comp.append(u)
            for nb in adj[u]:
                if nb not in visited:
                    visited.add(nb)
                    stack.append(nb)

        k = len(comp)
        if k == 2:
            a, b = comp
            score = float(X[a].multiply(X[b]).sum())
            if score >= similarity_threshold:
                member_groups.append(
                    norm_groups[unique_norms[a]] + norm_groups[unique_norms[b]]
                )
            else:
                member_groups.append(norm_groups[unique_norms[a]])
                member_groups.append(norm_groups[unique_norms[b]])
            continue

        sub_X = X[comp]
        sub_cos = (sub_X @ sub_X.T).toarray()
        iu, ju = np.triu_indices(k, k=1)
        sims = np.clip(sub_cos[iu, ju], 0.0, 1.0)
        p_comp = prices_arr[comp]
        p1, p2 = p_comp[iu], p_comp[ju]
        both_pos = (p1 > 0) & (p2 > 0)
        if np.any(both_pos):
            bad = np.zeros(len(iu), dtype=bool)
            bad[both_pos] = (
                np.maximum(p1[both_pos], p2[both_pos])
                / np.minimum(p1[both_pos], p2[both_pos])
            ) > max_price_ratio
            sims[bad] = 0.0
        dists = (1.0 - sims).astype(np.float64)
        Z = linkage(dists, method="average")
        labels = fcluster(Z, t=1.0 - similarity_threshold, criterion="distance")
        by_lbl: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for idx, lbl in zip(comp, labels):
            by_lbl[int(lbl)].extend(norm_groups[unique_norms[idx]])
        member_groups.extend(by_lbl.values())

    return member_groups + untitled_groups


def cluster_items_by_text(
    items: list[dict[str, Any]],
    *,
    min_cluster_size: int = 3,
    similarity_threshold: float = 0.78,
    max_price_ratio: float = 1.75,
) -> list[dict[str, Any]]:
    """Fast (~4s for 43k items) prefix-filtered Word TF-IDF + average-linkage clustering."""
    member_groups = _merge_groups_by_text(
        [[it] for it in items],
        similarity_threshold=similarity_threshold,
        max_price_ratio=max_price_ratio,
    )
    return assemble_clusters(member_groups, min_cluster_size=min_cluster_size)


def cluster_items_hybrid(
    items: list[dict[str, Any]],
    *,
    min_cluster_size: int = 3,
    max_hamming: int = 6,
    similarity_threshold: float = 0.78,
    max_price_ratio: float = 1.75,
) -> list[dict[str, Any]]:
    """Two-pass clustering: Pass 1 groups by image hash, Pass 2 merges by text.

    Catches both:
    - Identical reference photos with completely different titles (Pass 1)
    - Identical/near-identical titles with different color/lifestyle photos (Pass 2)
    """
    img_groups = _group_by_image_raw(items, max_hamming=max_hamming)
    merged_groups = _merge_groups_by_text(
        img_groups,
        similarity_threshold=similarity_threshold,
        max_price_ratio=max_price_ratio,
    )
    return assemble_clusters(merged_groups, min_cluster_size=min_cluster_size)


# Backward-compatible alias for benchmark_clustering.py
cluster_items = cluster_items_by_text


class ClusterTrackerDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS active_listings (
                listing_id TEXT PRIMARY KEY,
                item_id TEXT,
                sku TEXT,
                title TEXT,
                url TEXT,
                current_bid REAL,
                next_bid REAL,
                bid_count INTEGER,
                retail_price REAL,
                discount_percent INTEGER,
                location TEXT,
                start_timestamp INTEGER,
                end_timestamp INTEGER,
                starts_at TEXT,
                ends_at TEXT,
                image_url TEXT,
                img_sha256 TEXT,
                img_dhash TEXT,
                img_phash TEXT,
                first_indexed_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_active_listings_end_ts
                ON active_listings (end_timestamp);

            CREATE TABLE IF NOT EXISTS runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                total_items INTEGER NOT NULL,
                total_clusters INTEGER NOT NULL,
                new_clusters INTEGER NOT NULL,
                growing_clusters INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS known_clusters (
                cluster_id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_seen_date TEXT NOT NULL,
                last_seen_date TEXT NOT NULL,
                canonical_title TEXT NOT NULL,
                canonical_norm TEXT NOT NULL,
                canonical_phash TEXT,
                peak_count INTEGER NOT NULL,
                last_count INTEGER NOT NULL,
                seen_item_ids_json TEXT NOT NULL
            );
            """
        )
        # Lightweight migrations for existing DBs
        existing_cols = {
            r["name"]
            for r in self.conn.execute("PRAGMA table_info(active_listings)").fetchall()
        }
        for col in ("img_sha256", "img_dhash", "img_phash"):
            if col not in existing_cols:
                self.conn.execute(f"ALTER TABLE active_listings ADD COLUMN {col} TEXT")

        kc_cols = {
            r["name"]
            for r in self.conn.execute("PRAGMA table_info(known_clusters)").fetchall()
        }
        if "canonical_phash" not in kc_cols:
            self.conn.execute("ALTER TABLE known_clusters ADD COLUMN canonical_phash TEXT")

        self.conn.commit()

    def get_indexed_listing_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT listing_id FROM active_listings").fetchall()
        return {str(r["listing_id"]) for r in rows}

    def get_unhashed_listings(self) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            """
            SELECT listing_id, image_url
            FROM active_listings
            WHERE image_url IS NOT NULL AND image_url != ''
              AND (img_phash IS NULL OR img_dhash IS NULL)
            """
        ).fetchall()
        return [(str(r["listing_id"]), str(r["image_url"])) for r in rows]

    def update_image_hashes(self, rows: list[tuple[str, str, str, str]]) -> None:
        """Update (img_sha256, img_dhash, img_phash, listing_id) in batch."""
        if not rows:
            return
        self.conn.executemany(
            """
            UPDATE active_listings
            SET img_sha256 = ?, img_dhash = ?, img_phash = ?
            WHERE listing_id = ?
            """,
            rows,
        )
        self.conn.commit()

    def upsert_listings(self, items: list[dict[str, Any]], indexed_at: str) -> int:
        if not items:
            return 0
        rows = []
        for it in items:
            lid = it.get("listing_id") or it.get("item_id")
            if not lid:
                continue
            rows.append(
                (
                    str(lid),
                    str(it["item_id"]) if it.get("item_id") is not None else None,
                    it.get("sku"),
                    it.get("title"),
                    it.get("url"),
                    it.get("current_bid"),
                    it.get("next_bid"),
                    it.get("bid_count"),
                    it.get("retail_price"),
                    it.get("discount_percent"),
                    it.get("location"),
                    it.get("start_timestamp"),
                    it.get("end_timestamp"),
                    it.get("starts_at"),
                    it.get("ends_at"),
                    it.get("image_url"),
                    indexed_at,
                )
            )
        self.conn.executemany(
            """
            INSERT INTO active_listings (
                listing_id, item_id, sku, title, url, current_bid, next_bid,
                bid_count, retail_price, discount_percent, location,
                start_timestamp, end_timestamp, starts_at, ends_at,
                image_url, first_indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_id) DO UPDATE SET
                current_bid = excluded.current_bid,
                next_bid = excluded.next_bid,
                bid_count = excluded.bid_count,
                retail_price = excluded.retail_price,
                discount_percent = excluded.discount_percent,
                end_timestamp = excluded.end_timestamp,
                ends_at = excluded.ends_at
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    def prune_expired_listings(self, now_ts: int | None = None) -> int:
        if now_ts is None:
            now_ts = int(time.time())
        cur = self.conn.execute(
            "DELETE FROM active_listings WHERE end_timestamp IS NOT NULL AND end_timestamp <= ?",
            (now_ts,),
        )
        self.conn.commit()
        return cur.rowcount

    def load_active_listings(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM active_listings ORDER BY start_timestamp DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def run_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()
        return int(row["c"]) if row else 0

    def load_known_clusters(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM known_clusters").fetchall()
        result = []
        for r in rows:
            result.append(
                {
                    "cluster_id": r["cluster_id"],
                    "first_seen_date": r["first_seen_date"],
                    "last_seen_date": r["last_seen_date"],
                    "canonical_title": r["canonical_title"],
                    "canonical_norm": r["canonical_norm"],
                    "canonical_phash": r["canonical_phash"],
                    "peak_count": r["peak_count"],
                    "last_count": r["last_count"],
                    "seen_item_ids": set(json.loads(r["seen_item_ids_json"])),
                }
            )
        return result

    def match_and_update(
        self,
        clusters: list[dict[str, Any]],
        *,
        run_date: str,
        fetched_at: str,
        total_items: int,
        similarity_threshold: float = 0.78,
        max_hamming: int = 6,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        is_first_run = self.run_count() == 0
        known = self.load_known_clusters()

        item_to_known: dict[str, int] = {}
        for idx, kc in enumerate(known):
            for iid in kc["seen_item_ids"]:
                item_to_known[iid] = idx

        known_phashes = np.array(
            [
                int(kc["canonical_phash"], 16) if kc.get("canonical_phash") else 0
                for kc in known
            ],
            dtype=np.uint64,
        )
        known_has_phash = np.array(
            [bool(kc.get("canonical_phash")) for kc in known], dtype=bool
        )

        matched_known_idxs: set[int] = set()
        new_count = 0
        growing_count = 0

        for cl in clusters:
            cl_item_ids = set(cl["item_ids"])

            # 1. Match by shared item_ids first
            overlap_counts: Counter[int] = Counter()
            for iid in cl_item_ids:
                if iid in item_to_known:
                    overlap_counts[item_to_known[iid]] += 1

            best_k_idx: int | None = None
            if overlap_counts:
                for k_idx, _ in overlap_counts.most_common():
                    if k_idx not in matched_known_idxs:
                        best_k_idx = k_idx
                        break

            # 2. Match by perceptual image hash if available
            if best_k_idx is None and cl.get("canonical_phash") and len(known) > 0:
                cl_ph = np.uint64(int(cl["canonical_phash"], 16))
                dists = hamming64_vec(
                    known_phashes, np.full(len(known), cl_ph, dtype=np.uint64)
                )
                best_d = max_hamming + 1
                for k_idx in np.where(known_has_phash & (dists <= max_hamming))[0].tolist():
                    if k_idx not in matched_known_idxs and int(dists[k_idx]) < best_d:
                        best_d = int(dists[k_idx])
                        best_k_idx = k_idx

            # 3. Fall back to title token Jaccard
            if best_k_idx is None:
                best_score = 0.0
                for k_idx, kc in enumerate(known):
                    if k_idx in matched_known_idxs:
                        continue
                    score = title_similarity(
                        cl["canonical_norm"], kc["canonical_norm"]
                    )
                    if score >= similarity_threshold and score > best_score:
                        best_score = score
                        best_k_idx = k_idx

            if best_k_idx is None:
                cl["status"] = "NEW"
                cl["first_seen_date"] = run_date
                cl["new_item_count"] = cl["count"]
                cl["prev_count"] = 0
                new_count += 1
                if not dry_run:
                    cur = self.conn.execute(
                        """
                        INSERT INTO known_clusters (
                            first_seen_date, last_seen_date, canonical_title,
                            canonical_norm, canonical_phash, peak_count,
                            last_count, seen_item_ids_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            run_date,
                            cl["canonical_title"],
                            cl["canonical_norm"],
                            cl.get("canonical_phash"),
                            cl["count"],
                            cl["count"],
                            json.dumps(sorted(cl_item_ids)),
                        ),
                    )
                    cl["cluster_id"] = cur.lastrowid
            else:
                matched_known_idxs.add(best_k_idx)
                kc = known[best_k_idx]
                added_ids = cl_item_ids - kc["seen_item_ids"]
                cl["cluster_id"] = kc["cluster_id"]
                cl["first_seen_date"] = kc["first_seen_date"]
                cl["prev_count"] = kc["last_count"]
                cl["new_item_count"] = len(added_ids)

                if len(added_ids) > 0:
                    cl["status"] = "GROWING"
                    growing_count += 1
                else:
                    cl["status"] = "ONGOING"

                if not dry_run:
                    merged_ids = kc["seen_item_ids"] | cl_item_ids
                    peak = max(kc["peak_count"], cl["count"])
                    self.conn.execute(
                        """
                        UPDATE known_clusters
                        SET last_seen_date = ?,
                            canonical_title = ?,
                            canonical_norm = ?,
                            canonical_phash = COALESCE(?, canonical_phash),
                            peak_count = ?,
                            last_count = ?,
                            seen_item_ids_json = ?
                        WHERE cluster_id = ?
                        """,
                        (
                            run_date,
                            cl["canonical_title"],
                            cl["canonical_norm"],
                            cl.get("canonical_phash"),
                            peak,
                            cl["count"],
                            json.dumps(sorted(merged_ids)),
                            kc["cluster_id"],
                        ),
                    )

        if not dry_run:
            self.conn.execute(
                """
                INSERT INTO runs (
                    run_date, fetched_at, total_items, total_clusters,
                    new_clusters, growing_clusters
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_date,
                    fetched_at,
                    total_items,
                    len(clusters),
                    new_count,
                    growing_count,
                ),
            )
            self.conn.commit()

        return {
            "is_first_run": is_first_run,
            "run_date": run_date,
            "fetched_at": fetched_at,
            "total_items": total_items,
            "total_clusters": len(clusters),
            "new_clusters": new_count,
            "growing_clusters": growing_count,
            "ongoing_clusters": len(clusters) - new_count - growing_count,
        }

    def close(self) -> None:
        self.conn.close()


def sync_image_hashes(
    db: ClusterTrackerDB,
    *,
    workers: int = 32,
    timeout: float = 12.0,
) -> int:
    """Download & perceptually hash thumbnails for any unhashed active listings.

    Commits in batches of 1,000 so interrupted backfills resume cleanly.
    """
    todo = db.get_unhashed_listings()
    if not todo:
        return 0

    total = len(todo)
    print(
        f"Hashing {total:,} reference thumbnails with {workers} workers...",
        file=sys.stderr,
    )
    tls = threading.local()

    def _get_sess() -> requests.Session:
        s = getattr(tls, "sess", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": DEFAULT_USER_AGENT})
            tls.sess = s
        return s

    def _fetch_and_hash(item: tuple[str, str]) -> tuple[str, str, str, str] | None:
        lid, url = item
        sess = _get_sess()
        for attempt in range(2):
            try:
                resp = sess.get(url, timeout=timeout)
                if resp.status_code == 200 and resp.content:
                    sha, dh, ph = compute_image_hashes(resp.content)
                    return (sha, dh, ph, lid)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                    time.sleep(0.5)
                    continue
                return None
            except Exception:
                if attempt == 0:
                    time.sleep(0.3)
                    continue
                return None
        return None

    t0 = time.monotonic()
    done = 0
    ok_count = 0
    batch: list[tuple[str, str, str, str]] = []

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for res in pool.map(_fetch_and_hash, todo):
            done += 1
            if res is not None:
                batch.append(res)
                ok_count += 1
            if len(batch) >= 1000:
                db.update_image_hashes(batch)
                batch.clear()
            if done % 2000 == 0 or done == total:
                elapsed = time.monotonic() - t0
                rate = done / elapsed if elapsed > 0 else 0
                print(
                    f"  Images: {done:,}/{total:,} "
                    f"({done * 100 / total:.1f}%, {rate:.0f} img/s)",
                    file=sys.stderr,
                )

    if batch:
        db.update_image_hashes(batch)

    elapsed = time.monotonic() - t0
    print(
        f"Image hashing complete in {elapsed:.1f}s: "
        f"{ok_count:,}/{total:,} thumbnails hashed.",
        file=sys.stderr,
    )
    return ok_count


def sync_ca_catalog(
    db: ClusterTrackerDB,
    *,
    workers: int = 12,
    delay: float = 0.0,
    full_sync: bool = False,
    max_pages: int | None = None,
    overlap_pages_to_stop: int = 2,
) -> dict[str, int]:
    """Incrementally sync active CA listings (all prices) into SQLite."""
    client = JustBidClient(delay=delay)
    all_locations = client.fetch_locations()
    ca_ids = {
        loc["id"] for loc in all_locations if ", ca" in loc["address"].lower()
    }
    if not ca_ids:
        raise RuntimeError("Could not find any California pickup locations on JustBid.")
    client.set_locations(ca_ids, all_locations)
    ca_names = [f"{l['id']} ({l['name']})" for l in all_locations if l["id"] in ca_ids]
    print(f"CA locations selected: {', '.join(ca_names)}", file=sys.stderr)

    existing_ids = set() if full_sync else db.get_indexed_listing_ids()
    incremental = len(existing_ids) > 0 and not full_sync

    query_kwargs = {"sort": "newly_posted"}
    fetched_items: list[dict[str, Any]] = []
    seen_this_sync: set[str] = set()
    pages_fetched = 0
    new_listings_count = 0
    t0 = time.monotonic()

    if not incremental:
        print(
            "Initial full CA backfill: discovering total active pages...",
            file=sys.stderr,
        )
        total_pages = client.discover_last_page(**query_kwargs)
        pages_to_fetch = min(total_pages, max_pages) if max_pages else total_pages
        print(
            f"Found {total_pages} active CA pages (~{total_pages * 40:,} items). "
            f"Fetching {pages_to_fetch} page(s) with {workers} workers...",
            file=sys.stderr,
        )
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {
                pool.submit(client.fetch_page_items, p, **query_kwargs): p
                for p in range(1, pages_to_fetch + 1)
            }
            for fut in as_completed(futures):
                p = futures[fut]
                try:
                    page_items = fut.result()
                except Exception as exc:
                    print(f"Warning: failed page {p}: {exc}", file=sys.stderr)
                    page_items = []
                for it in page_items:
                    lid = str(it.get("listing_id") or it.get("item_id") or "")
                    if lid and lid not in seen_this_sync:
                        seen_this_sync.add(lid)
                        fetched_items.append(it)
                        if lid not in existing_ids:
                            new_listings_count += 1
                pages_fetched += 1
                if pages_fetched % 50 == 0 or pages_fetched == pages_to_fetch:
                    elapsed = time.monotonic() - t0
                    rate = pages_fetched / elapsed if elapsed > 0 else 0
                    print(
                        f"  Pages: {pages_fetched}/{pages_to_fetch} "
                        f"({pages_fetched * 100 / pages_to_fetch:.1f}%, {rate:.1f} pages/s)",
                        file=sys.stderr,
                    )
    else:
        print(
            f"Incremental CA sync ({len(existing_ids):,} already indexed). "
            f"Scanning newest pages until {overlap_pages_to_stop} consecutive overlap pages...",
            file=sys.stderr,
        )
        batch_size = max(4, workers)
        next_page = 1
        consecutive_overlap = 0
        done = False

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            while not done:
                end_page = next_page + batch_size - 1
                if max_pages is not None:
                    end_page = min(end_page, max_pages)
                    if next_page > end_page:
                        break

                batch_pages = list(range(next_page, end_page + 1))
                futures = {
                    pool.submit(client.fetch_page_items, p, **query_kwargs): p
                    for p in batch_pages
                }
                batch_results: dict[int, list[dict[str, Any]]] = {}
                for fut in as_completed(futures):
                    p = futures[fut]
                    try:
                        batch_results[p] = fut.result()
                    except Exception as exc:
                        print(f"Warning: failed page {p}: {exc}", file=sys.stderr)
                        batch_results[p] = []

                for p in batch_pages:
                    page_items = batch_results.get(p, [])
                    pages_fetched += 1
                    if not page_items:
                        done = True
                        break

                    new_on_page = 0
                    for it in page_items:
                        lid = str(it.get("listing_id") or it.get("item_id") or "")
                        if not lid:
                            continue
                        if lid not in existing_ids:
                            new_on_page += 1
                        if lid not in seen_this_sync:
                            seen_this_sync.add(lid)
                            fetched_items.append(it)
                            if lid not in existing_ids:
                                new_listings_count += 1

                    if new_on_page == 0:
                        consecutive_overlap += 1
                        if consecutive_overlap >= overlap_pages_to_stop:
                            done = True
                            break
                    else:
                        consecutive_overlap = 0

                elapsed = time.monotonic() - t0
                rate = pages_fetched / elapsed if elapsed > 0 else 0
                print(
                    f"  Scanned {pages_fetched} pages ({rate:.1f} pages/s) — "
                    f"{new_listings_count:,} new listings found so far",
                    file=sys.stderr,
                )
                next_page = end_page + 1

    indexed_at = datetime.now(timezone.utc).isoformat()
    db.upsert_listings(fetched_items, indexed_at)
    pruned = db.prune_expired_listings()
    elapsed = time.monotonic() - t0
    print(
        f"Sync complete in {elapsed:.1f}s: fetched {pages_fetched} pages, "
        f"{new_listings_count:,} new listings indexed, {pruned:,} expired listings pruned.",
        file=sys.stderr,
    )
    return {
        "pages_fetched": pages_fetched,
        "new_listings": new_listings_count,
        "pruned_expired": pruned,
    }


def fmt_money(val: float | None) -> str:
    if val is None:
        return "—"
    return f"${val:,.2f}" if val % 1 else f"${int(val):,}"


def generate_html_report(
    summary: dict[str, Any], clusters: list[dict[str, Any]], out_path: Path
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    status_rank = {"NEW": 0, "GROWING": 1, "ONGOING": 2}
    ordered = sorted(
        clusters,
        key=lambda c: (
            status_rank.get(c.get("status", "ONGOING"), 9),
            -c["count"],
            -(c["median_retail"] or 0),
        ),
    )

    cards_html: list[str] = []
    for cl in ordered:
        status = cl.get("status", "ONGOING")
        badge_cls = {
            "NEW": "badge-new",
            "GROWING": "badge-growing",
            "ONGOING": "badge-ongoing",
        }.get(status, "badge-ongoing")
        badge_text = (
            f"GROWING (+{cl['new_item_count']})"
            if status == "GROWING"
            else status
        )

        title_esc = html_lib.escape(cl["canonical_title"])
        thumb = cl.get("thumbnail_url") or ""
        thumb_html = (
            f'<img class="thumb" src="{html_lib.escape(thumb)}" loading="lazy" alt="" />'
            if thumb
            else '<div class="thumb thumb-empty">No image</div>'
        )

        retail_str = fmt_money(cl.get("median_retail"))
        if (
            cl.get("min_retail") is not None
            and cl.get("max_retail") is not None
            and cl["min_retail"] != cl["max_retail"]
        ):
            retail_str = (
                f"{fmt_money(cl['min_retail'])}–{fmt_money(cl['max_retail'])}"
            )

        locs_str = ", ".join(
            f"{html_lib.escape(loc)} ({cnt})"
            for loc, cnt in list(cl["locations"].items())[:4]
        )

        variants_html = ""
        if len(cl["distinct_titles"]) > 1:
            v_items = "".join(
                f"<li><b>{v['count']}×</b> {html_lib.escape(v['title'])}</li>"
                for v in cl["distinct_titles"][:6]
            )
            variants_html = f'<ul class="variants">{v_items}</ul>'

        item_rows = []
        for it in cl["items"][:50]:
            url = html_lib.escape(it.get("url") or "#")
            bid = fmt_money(it.get("current_bid"))
            ret = fmt_money(it.get("retail_price"))
            loc = html_lib.escape(it.get("location") or "")
            ends = html_lib.escape((it.get("ends_at") or "")[:16].replace("T", " "))
            it_title = html_lib.escape(it.get("title") or "")
            item_rows.append(
                f'<tr><td><a href="{url}" target="_blank" rel="noopener">{bid}</a></td>'
                f"<td>{ret}</td><td>{loc}</td><td>{ends} UTC</td>"
                f'<td><a href="{url}" target="_blank" rel="noopener">{it_title}</a></td></tr>'
            )
        more_note = (
            f"<p class='muted'>Showing first 50 of {cl['count']} items.</p>"
            if cl["count"] > 50
            else ""
        )

        all_titles_blob = " ".join(v["title"] for v in cl["distinct_titles"])
        search_blob = html_lib.escape(
            (all_titles_blob + " " + " ".join(cl["locations"].keys())).lower()
        )
        median_retail_num = cl.get("median_retail") or 0
        min_bid_num = cl.get("min_current_bid") if cl.get("min_current_bid") is not None else 1e9
        discount_num = (
            round((1.0 - min_bid_num / median_retail_num) * 100.0, 1)
            if median_retail_num > 0 and min_bid_num < 1e9
            else 0.0
        )
        earliest_end_ts = min(
            (it["end_timestamp"] for it in cl["items"] if it.get("end_timestamp")),
            default=9999999999,
        )
        locs_pipe = html_lib.escape("|" + "|".join(cl["locations"].keys()) + "|")
        s_rank = status_rank.get(status, 9)
        new_cnt = cl.get("new_item_count") or 0

        cards_html.append(
            f"""
            <article class="card"
              data-status="{status}"
              data-status-rank="{s_rank}"
              data-count="{cl['count']}"
              data-new-count="{new_cnt}"
              data-retail="{median_retail_num}"
              data-min-bid="{min_bid_num}"
              data-discount="{discount_num}"
              data-earliest-end="{earliest_end_ts}"
              data-locations="{locs_pipe}"
              data-search="{search_blob}">
              <div class="card-main">
                {thumb_html}
                <div class="card-body">
                  <div class="card-top">
                    <span class="badge {badge_cls}">{badge_text}</span>
                    <span class="pill count-pill">{cl['count']} units</span>
                    <span class="pill">Retail {retail_str}</span>
                    <span class="pill bid-pill">Lowest bid {fmt_money(cl.get('min_current_bid'))}</span>
                    <span class="muted">First seen {html_lib.escape(cl.get('first_seen_date', ''))}</span>
                  </div>
                  <h3>{title_esc}</h3>
                  <div class="meta">Locations: {locs_str}</div>
                  {variants_html}
                  <details>
                    <summary>View {cl['count']} active listings (sorted by lowest bid)</summary>
                    <table>
                      <thead><tr><th>Bid</th><th>Retail</th><th>Location</th><th>Ends</th><th>Listing</th></tr></thead>
                      <tbody>{"".join(item_rows)}</tbody>
                    </table>
                    {more_note}
                  </details>
                </div>
              </div>
            </article>
            """
        )

    all_locs = sorted({loc for cl in ordered for loc in cl["locations"].keys()})
    loc_options = "".join(
        f'<option value="{html_lib.escape(loc)}">{html_lib.escape(loc)}</option>'
        for loc in all_locs
    )

    first_run_banner = (
        '<div class="banner">Baseline run — all clusters recorded for the first time. '
        "Future daily runs will highlight only newly appearing and growing clusters.</div>"
        if summary["is_first_run"]
        else ""
    )

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>JustBid CA Liquidation Clusters — {html_lib.escape(summary['run_date'])}</title>
  <style>
    :root {{ --bg: #f6f7f9; --card: #fff; --border: #e2e8f0; --text: #0f172a; --muted: #64748b; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); }}
    header {{ background: #0f172a; color: #fff; padding: 20px 24px; }}
    header h1 {{ margin: 0 0 6px; font-size: 22px; }}
    header .sub {{ color: #94a3b8; font-size: 14px; }}
    .wrap {{ max-width: 1150px; margin: 20px auto; padding: 0 16px 48px; }}
    .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px; }}
    .stat {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }}
    .stat .num {{ font-size: 26px; font-weight: 700; }}
    .stat .lbl {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }}
    .banner {{ background: #eff6ff; border: 1px solid #bfdbfe; color: #1e40af; padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; }}
    .toolbar {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px; margin-bottom: 14px; display: flex; flex-direction: column; gap: 10px; }}
    .controls {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
    .tab {{ border: 1px solid var(--border); background: #f8fafc; padding: 7px 13px; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 13px; }}
    .tab.active {{ background: #0f172a; color: #fff; border-color: #0f172a; }}
    .ctrl-input, .ctrl-select {{ padding: 7px 10px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px; background: #fff; color: var(--text); }}
    #search {{ flex: 1; min-width: 220px; }}
    .num-input {{ width: 118px; }}
    .btn-reset {{ border: 1px solid var(--border); background: #f8fafc; padding: 7px 12px; border-radius: 8px; cursor: pointer; font-size: 13px; color: var(--muted); }}
    .btn-reset:hover {{ background: #e2e8f0; color: var(--text); }}
    .result-bar {{ display: flex; justify-content: space-between; align-items: center; font-size: 13px; color: var(--muted); margin-bottom: 10px; padding: 0 2px; }}
    .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px; margin-bottom: 12px; }}
    .card-main {{ display: flex; gap: 16px; align-items: flex-start; }}
    .thumb {{ width: 96px; height: 96px; object-fit: contain; border-radius: 8px; border: 1px solid var(--border); background: #fff; flex-shrink: 0; }}
    .thumb-empty {{ display: flex; align-items: center; justify-content: center; color: var(--muted); font-size: 12px; }}
    .card-body {{ flex: 1; min-width: 0; }}
    .card-top {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-bottom: 6px; }}
    .badge {{ font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 999px; letter-spacing: .03em; }}
    .badge-new {{ background: #dcfce7; color: #166534; }}
    .badge-growing {{ background: #fef3c7; color: #92400e; }}
    .badge-ongoing {{ background: #e2e8f0; color: #334155; }}
    .pill {{ font-size: 12px; background: #f1f5f9; padding: 3px 8px; border-radius: 6px; font-weight: 600; }}
    .count-pill {{ background: #dbeafe; color: #1e40af; }}
    .bid-pill {{ background: #ecfdf5; color: #065f46; }}
    h3 {{ margin: 0 0 6px; font-size: 16px; line-height: 1.35; }}
    .meta {{ font-size: 13px; color: var(--muted); margin-bottom: 6px; }}
    .muted {{ font-size: 12px; color: var(--muted); }}
    .variants {{ margin: 6px 0 8px; padding-left: 18px; font-size: 12px; color: #475569; }}
    details {{ margin-top: 8px; }}
    summary {{ cursor: pointer; font-size: 13px; font-weight: 600; color: #2563eb; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; }}
    th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); }}
    th {{ background: #f8fafc; font-size: 12px; color: var(--muted); }}
    a {{ color: #2563eb; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .hidden {{ display: none; }}
  </style>
</head>
<body>
  <header>
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
      <div>
        <h1>JustBid CA Liquidation Clusters — {html_lib.escape(summary['run_date'])}</h1>
        <div class="sub">Synced at {html_lib.escape(summary['fetched_at'])}</div>
      </div>
      <div style="font-size:13px;">
        <a href="../" style="color:#93c5fd;margin-right:12px;">← JustBid Tools</a>
        <a href="../extension/" style="color:#93c5fd;">Condition Extension</a>
      </div>
    </div>
  </header>
  <div class="wrap">
    {first_run_banner}
    <div class="stats">
      <div class="stat"><div class="num">{summary['total_items']:,}</div><div class="lbl">Active CA Items</div></div>
      <div class="stat"><div class="num">{summary['total_clusters']:,}</div><div class="lbl">Total Clusters</div></div>
      <div class="stat"><div class="num" style="color:#16a34a">{summary['new_clusters']:,}</div><div class="lbl">New Today</div></div>
      <div class="stat"><div class="num" style="color:#d97706">{summary['growing_clusters']:,}</div><div class="lbl">Growing Today</div></div>
      <div class="stat"><div class="num">{summary['ongoing_clusters']:,}</div><div class="lbl">Ongoing</div></div>
    </div>
    <div class="toolbar">
      <div class="controls">
        <button class="tab active" data-filter="ALL">All ({summary['total_clusters']})</button>
        <button class="tab" data-filter="NEW">New ({summary['new_clusters']})</button>
        <button class="tab" data-filter="GROWING">Growing ({summary['growing_clusters']})</button>
        <button class="tab" data-filter="ONGOING">Ongoing ({summary['ongoing_clusters']})</button>
        <input id="search" class="ctrl-input" type="search" placeholder="Search title, variant, or location..." />
      </div>
      <div class="controls">
        <select id="sortBy" class="ctrl-select" title="Sort clusters">
          <option value="default">Sort: Status + Most Units</option>
          <option value="units_desc">Sort: Most Units</option>
          <option value="retail_desc">Sort: Highest Retail $</option>
          <option value="retail_asc">Sort: Lowest Retail $</option>
          <option value="bid_asc">Sort: Lowest Current Bid</option>
          <option value="discount_desc">Sort: Biggest Discount %</option>
          <option value="ending_asc">Sort: Ending Soonest</option>
          <option value="new_desc">Sort: Most New Units Added</option>
        </select>
        <select id="locFilter" class="ctrl-select" title="Filter by pickup location">
          <option value="">All Locations</option>
          {loc_options}
        </select>
        <input id="minUnits" class="ctrl-input num-input" type="number" min="1" step="1" placeholder="Min units" />
        <input id="minRetail" class="ctrl-input num-input" type="number" min="0" step="10" placeholder="Min retail $" />
        <input id="maxRetail" class="ctrl-input num-input" type="number" min="0" step="10" placeholder="Max retail $" />
        <input id="maxBid" class="ctrl-input num-input" type="number" min="0" step="5" placeholder="Max low bid $" />
        <button id="resetBtn" class="btn-reset" type="button">Reset</button>
      </div>
    </div>
    <div class="result-bar">
      <span id="visibleCount">Showing {summary['total_clusters']:,} of {summary['total_clusters']:,} clusters</span>
    </div>
    <div id="list">{"".join(cards_html)}</div>
  </div>
  <script>
    let currentFilter = 'ALL';
    const listEl = document.getElementById('list');
    const tabs = document.querySelectorAll('.tab');
    const search = document.getElementById('search');
    const sortBy = document.getElementById('sortBy');
    const locFilter = document.getElementById('locFilter');
    const minUnits = document.getElementById('minUnits');
    const minRetail = document.getElementById('minRetail');
    const maxRetail = document.getElementById('maxRetail');
    const maxBid = document.getElementById('maxBid');
    const resetBtn = document.getElementById('resetBtn');
    const visibleCount = document.getElementById('visibleCount');
    const cards = Array.from(document.querySelectorAll('.card'));
    const totalCards = cards.length;

    function numVal(el, fallback) {{
      const v = parseFloat(el.value);
      return Number.isFinite(v) ? v : fallback;
    }}

    function applyFilter() {{
      const q = search.value.trim().toLowerCase();
      const loc = locFilter.value;
      const minU = numVal(minUnits, 0);
      const minR = numVal(minRetail, 0);
      const maxR = numVal(maxRetail, Infinity);
      const maxB = numVal(maxBid, Infinity);

      let shown = 0;
      cards.forEach(c => {{
        const statusOk = currentFilter === 'ALL' || c.dataset.status === currentFilter;
        const textOk = !q || c.dataset.search.includes(q);
        const locOk = !loc || c.dataset.locations.includes('|' + loc + '|');
        const unitsOk = (parseInt(c.dataset.count, 10) || 0) >= minU;
        const retail = parseFloat(c.dataset.retail) || 0;
        const retailOk = retail >= minR && retail <= maxR;
        const bid = parseFloat(c.dataset.minBid);
        const bidOk = !Number.isFinite(maxB) || (Number.isFinite(bid) && bid <= maxB);
        const ok = statusOk && textOk && locOk && unitsOk && retailOk && bidOk;
        c.classList.toggle('hidden', !ok);
        if (ok) shown++;
      }});
      visibleCount.textContent = `Showing ${{shown.toLocaleString()}} of ${{totalCards.toLocaleString()}} clusters`;
    }}

    function applySort() {{
      const mode = sortBy.value;
      const cmp = {{
        default: (a, b) =>
          (a.dataset.statusRank - b.dataset.statusRank) ||
          (b.dataset.count - a.dataset.count) ||
          (b.dataset.retail - a.dataset.retail),
        units_desc: (a, b) =>
          (b.dataset.count - a.dataset.count) || (b.dataset.retail - a.dataset.retail),
        retail_desc: (a, b) =>
          (b.dataset.retail - a.dataset.retail) || (b.dataset.count - a.dataset.count),
        retail_asc: (a, b) =>
          (a.dataset.retail - b.dataset.retail) || (b.dataset.count - a.dataset.count),
        bid_asc: (a, b) =>
          (a.dataset.minBid - b.dataset.minBid) || (b.dataset.retail - a.dataset.retail),
        discount_desc: (a, b) =>
          (b.dataset.discount - a.dataset.discount) || (b.dataset.retail - a.dataset.retail),
        ending_asc: (a, b) =>
          (a.dataset.earliestEnd - b.dataset.earliestEnd) || (b.dataset.count - a.dataset.count),
        new_desc: (a, b) =>
          (b.dataset.newCount - a.dataset.newCount) || (b.dataset.count - a.dataset.count),
      }}[mode] || (() => 0);

      cards.sort(cmp);
      const frag = document.createDocumentFragment();
      cards.forEach(c => frag.appendChild(c));
      listEl.appendChild(frag);
    }}

    tabs.forEach(t => t.addEventListener('click', () => {{
      tabs.forEach(x => x.classList.remove('active'));
      t.classList.add('active');
      currentFilter = t.dataset.filter;
      applyFilter();
    }}));

    [search, minUnits, minRetail, maxRetail, maxBid].forEach(el =>
      el.addEventListener('input', applyFilter)
    );
    locFilter.addEventListener('change', applyFilter);
    sortBy.addEventListener('change', applySort);
    resetBtn.addEventListener('click', () => {{
      currentFilter = 'ALL';
      tabs.forEach(x => x.classList.toggle('active', x.dataset.filter === 'ALL'));
      search.value = '';
      locFilter.value = '';
      minUnits.value = '';
      minRetail.value = '';
      maxRetail.value = '';
      maxBid.value = '';
      sortBy.value = 'default';
      applySort();
      applyFilter();
    }});
  </script>
</body>
</html>"""

    out_path.write_text(page_html, encoding="utf-8")


def print_terminal_summary(
    summary: dict[str, Any], clusters: list[dict[str, Any]], top_n: int = 20
) -> None:
    print(f"\n=== JustBid CA Cluster Report ({summary['run_date']}) ===")
    print(
        f"Active CA items: {summary['total_items']:,} | "
        f"Clusters: {summary['total_clusters']} "
        f"(NEW: {summary['new_clusters']}, "
        f"GROWING: {summary['growing_clusters']}, "
        f"ONGOING: {summary['ongoing_clusters']})"
    )
    if summary["is_first_run"]:
        print(
            "Note: First run recorded as baseline. Tomorrow's run will highlight newly appearing clusters."
        )

    for label, status in [("NEW CLUSTERS", "NEW"), ("GROWING CLUSTERS", "GROWING")]:
        subset = [c for c in clusters if c.get("status") == status]
        if not subset:
            continue
        print(f"\n--- {label} (showing up to {top_n} of {len(subset)}) ---")
        for cl in subset[:top_n]:
            delta = f" (+{cl['new_item_count']} new)" if status == "GROWING" else ""
            cheapest_url = cl["items"][0].get("url", "") if cl["items"] else ""
            print(
                f"  [{cl['count']:3d} units{delta}] "
                f"Retail {fmt_money(cl['median_retail'])} | "
                f"Low bid {fmt_money(cl['min_current_bid'])} | "
                f"{cl['canonical_title'][:72]}"
            )
            if cheapest_url:
                print(f"      -> {cheapest_url}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Incrementally sync the active CA JustBid catalog, cluster liquidation duplicates by image or text, and track new daily clusters."
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Directory for SQLite DB and daily reports (default: data)",
    )
    parser.add_argument(
        "--cluster-by",
        choices=("hybrid", "image", "text"),
        default="hybrid",
        help="Clustering mode: hybrid (image pass + text merge pass, default), image only, or text only",
    )
    parser.add_argument(
        "--image-hamming",
        type=int,
        default=6,
        help="Max 64-bit pHash Hamming distance for image clustering (default: 6; 0=exact photo, 10-12=include sibling variants)",
    )
    parser.add_argument(
        "--full-sync",
        action="store_true",
        help="Force a full re-scan of all active CA pages instead of incremental sync",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip fetching catalog pages (still hashes any unhashed thumbnails in image mode)",
    )
    parser.add_argument(
        "--min-cluster-size",
        type=int,
        default=3,
        help="Minimum number of active items to count as a cluster (default: 3)",
    )
    parser.add_argument(
        "--similarity",
        type=float,
        default=0.78,
        help="Text clustering similarity threshold 0..1 (default: 0.78)",
    )
    parser.add_argument(
        "--max-price-ratio",
        type=float,
        default=1.75,
        help="Max retail price ratio allowed in text clustering (default: 1.75)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional page cap when syncing (useful for quick testing)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=12,
        help="Concurrent HTTP workers for catalog pages (default: 12)",
    )
    parser.add_argument(
        "--image-workers",
        type=int,
        default=32,
        help="Concurrent HTTP workers for thumbnail hashing (default: 32)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Optional per-request delay in seconds for catalog pages",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not record this run into cluster history",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Number of clusters per section to print in terminal summary (default: 20)",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    run_date = datetime.now().strftime("%Y-%m-%d")
    fetched_at = datetime.now(timezone.utc).isoformat()

    db = ClusterTrackerDB(data_dir / "tracker.db")
    try:
        if not args.no_fetch:
            sync_ca_catalog(
                db,
                workers=args.workers,
                delay=args.delay,
                full_sync=args.full_sync,
                max_pages=args.max_pages,
            )
        else:
            pruned = db.prune_expired_listings()
            print(
                f"Skipped catalog page fetch (--no-fetch). Pruned {pruned:,} expired listings.",
                file=sys.stderr,
            )

        if args.cluster_by in ("hybrid", "image"):
            sync_image_hashes(db, workers=args.image_workers)

        items = db.load_active_listings()
        t0 = time.monotonic()
        if args.cluster_by == "hybrid":
            clusters = cluster_items_hybrid(
                items,
                min_cluster_size=args.min_cluster_size,
                max_hamming=args.image_hamming,
                similarity_threshold=args.similarity,
                max_price_ratio=args.max_price_ratio,
            )
        elif args.cluster_by == "image":
            clusters = cluster_items_by_image(
                items,
                min_cluster_size=args.min_cluster_size,
                max_hamming=args.image_hamming,
            )
        else:
            clusters = cluster_items_by_text(
                items,
                min_cluster_size=args.min_cluster_size,
                similarity_threshold=args.similarity,
                max_price_ratio=args.max_price_ratio,
            )
        print(
            f"Clustered {len(items):,} active CA items by {args.cluster_by} into "
            f"{len(clusters)} clusters (size >= {args.min_cluster_size}) "
            f"in {time.monotonic() - t0:.2f}s.",
            file=sys.stderr,
        )

        summary = db.match_and_update(
            clusters,
            run_date=run_date,
            fetched_at=fetched_at,
            total_items=len(items),
            similarity_threshold=args.similarity,
            max_hamming=args.image_hamming,
            dry_run=args.dry_run,
        )
    finally:
        db.close()

    reports_dir = data_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_report = reports_dir / f"{run_date}.json"
    html_report = reports_dir / f"{run_date}.html"

    json_report.write_text(
        json.dumps(
            {"summary": summary, "clusters": clusters},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    generate_html_report(summary, clusters, html_report)

    latest_html = reports_dir / "latest.html"
    latest_json = reports_dir / "latest.json"
    for link_path, target_name in (
        (latest_html, html_report.name),
        (latest_json, json_report.name),
    ):
        tmp_link = link_path.with_suffix(link_path.suffix + ".tmp")
        tmp_link.unlink(missing_ok=True)
        tmp_link.symlink_to(target_name)
        tmp_link.replace(link_path)

    print_terminal_summary(summary, clusters, top_n=args.top)
    print(
        f"\nReports written:\n  HTML: {html_report} (symlink: {latest_html})\n"
        f"  JSON: {json_report} (symlink: {latest_json})"
    )
    if args.dry_run:
        print("  (dry-run: cluster history was not modified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
