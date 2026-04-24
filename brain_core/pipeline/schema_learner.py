#!/opt/homebrew/bin/python3
"""CLS schema learner (2026-04-17) — MVP.

Inspired by friend's SECONDBRAIN_SCHEMA_LEARNER. Uses spectral clustering on
the `atom_coactivation` graph to discover latent clusters of semantically
related atoms. Each cluster becomes a `canonical_compaction` candidate —
proposing "these atoms probably belong in one canonical page".

Why spectral clustering over k-means / DBSCAN:
  - Co-activation is a graph signal (atoms that surface together for the same
    query). Graph Laplacian eigendecomposition respects community structure.
  - No need to guess a cluster count up front — eigengap heuristic picks k.
  - Handles non-convex clusters (k-means assumes blobs in Euclidean space).

Scope of MVP:
  - Read atom_coactivation rows with n_events >= 2 (noise floor)
  - Build sparse adjacency matrix
  - Spectral embedding → k-means on eigenvectors → cluster labels
  - For each cluster of size 3+, emit a `canonical_compaction_candidate` row
    into brain_config (inspected manually; destructive merge stays human-gated)

Runs Sunday 04:45 before canonical_compaction at 06:00 so the human-review
queue has candidates ready. Not scheduled tighter because co-activation signal
changes slowly (needs a few sleep_consolidate cycles to populate).

Dependencies: scipy + scikit-learn (already in brain venv via .venv).
"""

from __future__ import annotations

import os

# joblib/loky spam the stderr with a sysctl-not-found traceback on every task
# because launchd strips /usr/sbin from PATH. Set the CPU count explicitly so
# joblib skips the probe entirely and keeps err logs readable.
os.environ.setdefault("LOKY_MAX_CPU_COUNT", "8")

import json
import logging
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from config import AUTONOMY_DB, BRAIN_LOGS_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")

BRAIN_DB = BRAIN_LOGS_DIR / "brain.db"
MIN_EDGE_WEIGHT = 2  # drop co-activation edges with n_events < 2 (noise)
MIN_CLUSTER_SIZE = 3  # clusters smaller than this aren't worth proposing
MAX_CLUSTERS = 10  # cap output so one bad run can't flood config
K_MIN = 2  # never fewer than 2 clusters
K_MAX = 12  # never more than 12 clusters

log = logging.getLogger("brain.schema_learner")


def _load_edges() -> list[tuple[str, str, int]]:
    if not BRAIN_DB.exists():
        return []
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        rows = conn.execute(
            "SELECT atom_a_id, atom_b_id, n_events FROM atom_coactivation "
            "WHERE n_events >= ? ORDER BY n_events DESC",
            (MIN_EDGE_WEIGHT,),
        ).fetchall()
    finally:
        conn.close()
    return [(a, b, int(n)) for a, b, n in rows]


def _load_atom_texts(atom_ids: list[str]) -> dict[str, str]:
    if not atom_ids:
        return {}
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        placeholders = ",".join("?" for _ in atom_ids)
        rows = conn.execute(
            f"SELECT id, text FROM atoms WHERE id IN ({placeholders})",
            atom_ids,
        ).fetchall()
    finally:
        conn.close()
    return {a: (t or "")[:120] for a, t in rows}


def _pick_k(eigenvalues, n_nodes: int) -> int:
    """Eigengap heuristic: pick k where the gap between consecutive
    sorted eigenvalues is largest (after skipping near-zero eigenvalues that
    correspond to disconnected components — a fully disconnected graph has
    one zero per component, not just the single leading zero we used to skip).
    """
    import numpy as np

    sorted_vals = np.sort(eigenvalues)[: K_MAX + 2]
    # 2026-04-17 fix: skip ALL near-zero eigenvalues, not just the first. In
    # a disconnected graph with c components, the Laplacian has c zeros; if
    # we only skip one, the largest "gap" lands between remaining zeros and
    # the first non-trivial eigenvalue, forcing k == component count.
    nontrivial = sorted_vals[sorted_vals > 1e-6]
    if len(nontrivial) < 2:
        return K_MIN
    gaps = np.diff(nontrivial)
    if len(gaps) == 0:
        return K_MIN
    # Offset by count of zero eigenvalues skipped so index maps back to k.
    # 2026-04-18 fix: previous `+ 2` over-counted by one. argmax on gaps[:n-1]
    # returns index i of the largest jump between nontrivial[i] and nontrivial[i+1],
    # so the eigengap indicates k = n_zero + i + 1 clusters (eigenvalues
    # {0..n_zero-1, nontrivial[0..i]} span the signal subspace). Was
    # systematically over-fragmenting canonical compaction clusters by 1.
    n_zero = len(sorted_vals) - len(nontrivial)
    k = int(np.argmax(gaps)) + n_zero + 1
    return max(K_MIN, min(k, min(K_MAX, max(2, n_nodes // MIN_CLUSTER_SIZE))))


def cluster() -> dict:
    """Run spectral clustering + write candidates. Returns summary dict."""
    t_start = time.time()
    edges = _load_edges()
    if len(edges) < MIN_CLUSTER_SIZE:
        return {"status": "insufficient_edges", "edges": len(edges)}

    import numpy as np
    from scipy.sparse import csr_matrix
    from sklearn.cluster import SpectralClustering

    atoms: dict[str, int] = {}
    row_idx: list[int] = []
    col_idx: list[int] = []
    data: list[float] = []
    for a, b, w in edges:
        if a not in atoms:
            atoms[a] = len(atoms)
        if b not in atoms:
            atoms[b] = len(atoms)
        ia, ib = atoms[a], atoms[b]
        row_idx.extend([ia, ib])  # symmetric
        col_idx.extend([ib, ia])
        data.extend([float(w), float(w)])

    n = len(atoms)
    if n < MIN_CLUSTER_SIZE:
        return {"status": "insufficient_nodes", "nodes": n}
    # 2026-04-17 OOM cap: dense Laplacian + SpectralClustering.fit_predict both
    # materialize n×n float64 arrays. At n=2000 that's 32MB — fine. At n=10k
    # it's 800MB — Mac Studio OOMs. Bail with a status before we blow memory.
    if n > 2000:
        return {"status": "too_many_nodes", "nodes": n}

    adj = csr_matrix((data, (row_idx, col_idx)), shape=(n, n))

    # Pick k via eigengap heuristic on normalized graph Laplacian
    from scipy.sparse.csgraph import laplacian

    lap = laplacian(adj, normed=True).toarray()
    eigenvalues = np.linalg.eigvalsh(lap)
    k = _pick_k(eigenvalues, n)

    try:
        clusterer = SpectralClustering(
            n_clusters=k,
            affinity="precomputed",
            assign_labels="kmeans",
            random_state=42,
        )
        labels = clusterer.fit_predict(adj.toarray())
    except Exception as exc:
        return {"status": "cluster_failed", "error": str(exc)[:200]}

    # Group atoms by label
    inv = {idx: aid for aid, idx in atoms.items()}
    clusters: dict[int, list[str]] = {}
    for idx, label in enumerate(labels):
        clusters.setdefault(int(label), []).append(inv[idx])

    # Keep only clusters with MIN_CLUSTER_SIZE+; enrich with atom text preview
    valid_clusters = [
        {"atoms": atom_ids, "size": len(atom_ids)}
        for atom_ids in clusters.values()
        if len(atom_ids) >= MIN_CLUSTER_SIZE
    ]
    valid_clusters.sort(key=lambda c: -c["size"])
    valid_clusters = valid_clusters[:MAX_CLUSTERS]

    # Look up preview text for all atoms in surviving clusters
    all_ids = [a for c in valid_clusters for a in c["atoms"]]
    previews = _load_atom_texts(all_ids)
    for c in valid_clusters:
        c["preview"] = [{"id": a, "text": previews.get(a, "")} for a in c["atoms"][:5]]

    # Persist to brain_config so canonical_compaction (Sun 06:00) can consume
    conn = None
    try:
        conn = sqlite3.connect(str(AUTONOMY_DB))
        conn.execute(
            "INSERT OR REPLACE INTO brain_config (key, value, updated_at, updated_by) " "VALUES (?, ?, ?, ?)",
            (
                "schema_learner.candidates",
                json.dumps(
                    {
                        "generated_at": datetime.now(UTC).isoformat(),
                        "n_edges": len(edges),
                        "n_nodes": n,
                        "k": k,
                        "clusters": valid_clusters,
                    },
                    ensure_ascii=False,
                ),
                datetime.now(UTC).isoformat(),
                "schema_learner",
            ),
        )
        conn.commit()
    except Exception as exc:
        log.warning("candidate write failed: %s", exc)
    finally:
        if conn is not None:
            conn.close()

    return {
        "status": "ok",
        "n_edges": len(edges),
        "n_nodes": n,
        "k": k,
        "clusters_found": len(valid_clusters),
        "duration_ms": int((time.time() - t_start) * 1000),
    }


if __name__ == "__main__":
    from _watchdog import arm as _arm_watchdog

    # Spectral clustering on up to 2000-node graphs plus sklearn k-means —
    # bounded-risk but adding a cap so a degenerate eigendecomposition
    # can't wedge the scheduler subprocess reaper.
    _arm_watchdog(600, tag="schema_learner")
    result = cluster()
    print(json.dumps(result, indent=2, ensure_ascii=False))
