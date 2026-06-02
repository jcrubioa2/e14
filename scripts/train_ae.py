"""One-class conv-autoencoder proof for per-digit anomaly detection.

Trains ONLY on clean digit slots (abundant, CV-identified) and scores anomalies by
reconstruction error. The critical test is GENERALIZATION ACROSS WRITERS: clean
digits are split by acta, the model never sees the test actas' writers, and we ask
whether the known anomalies poke above the held-out-writer clean error spread.

Metrics: mean MSE (global deviation) and max-local error (a concentrated blob, which
is what a small fused dot produces).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.harvest_digits import (  # noqa: E402
    SZ, norm_slot, slot_binaries, slot_from,
)
from e14detector.classifier import classify_slot  # noqa: E402
from e14detector.cv_features import extract_slot_features  # noqa: E402
from e14detector.schemas import SlotClass  # noqa: E402

torch.manual_seed(0)
np.random.seed(0)


def harvest_by_doc(db_path: str) -> dict[str, list[np.ndarray]]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT document_id, raw_crop_path FROM vote_fields "
        "WHERE row_type='candidate' AND final_classification='CLEAN' AND raw_crop_path IS NOT NULL"
    ).fetchall()
    con.close()
    by_doc: dict[str, list[np.ndarray]] = {}
    for r in rows:
        p = r["raw_crop_path"]
        if not p or not Path(p).exists():
            continue
        try:
            bins = slot_binaries(p)
        except Exception:
            continue
        for bb in bins:
            if classify_slot(extract_slot_features(bb)).slot_class != SlotClass.DIGIT:
                continue
            n = norm_slot(bb)
            if n is not None:
                by_doc.setdefault(r["document_id"], []).append(n)
    return by_doc


class ConvAE(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.enc = nn.Sequential(
            nn.Conv2d(1, 16, 3, 2, 1), nn.ReLU(),   # 32->16
            nn.Conv2d(16, 32, 3, 2, 1), nn.ReLU(),  # 16->8
            nn.Conv2d(32, 64, 3, 2, 1), nn.ReLU(),  # 8->4
        )
        self.dec = nn.Sequential(
            nn.ConvTranspose2d(64, 32, 4, 2, 1), nn.ReLU(),
            nn.ConvTranspose2d(32, 16, 4, 2, 1), nn.ReLU(),
            nn.ConvTranspose2d(16, 1, 4, 2, 1), nn.Sigmoid(),
        )

    def forward(self, x):
        return self.dec(self.enc(x))


def augment(x: torch.Tensor) -> torch.Tensor:
    # mild shift so the AE learns shape, not exact placement (writer robustness)
    dx, dy = np.random.randint(-2, 3), np.random.randint(-2, 3)
    return torch.roll(x, shifts=(dy, dx), dims=(2, 3))


def errors(model, X: np.ndarray):
    model.eval()
    with torch.no_grad():
        t = torch.tensor(X).unsqueeze(1)
        rec = model(t)
        err = (rec - t) ** 2
        mean_e = err.mean(dim=(1, 2, 3)).numpy()
        # max-local: 5x5 average-pooled error map, take the peak
        pooled = nn.functional.avg_pool2d(err, 5, 1, 2)
        maxloc = pooled.amax(dim=(1, 2, 3)).numpy()
    return mean_e, maxloc


def pct(val: float, dist: np.ndarray) -> float:
    return 100.0 * (dist < val).mean()


def main() -> None:
    by_doc = harvest_by_doc("data/_digitset/results/results.sqlite")
    docs = sorted(by_doc)
    print(f"clean digit slots from {len(docs)} actas, "
          f"{sum(len(v) for v in by_doc.values())} slots total")
    rng = np.random.default_rng(0)
    rng.shuffle(docs)
    cut = int(len(docs) * 0.8)
    train_docs, test_docs = docs[:cut], docs[cut:]
    Xtr = np.concatenate([by_doc[d] for d in train_docs], 0)
    Xte = np.concatenate([by_doc[d] for d in test_docs], 0)  # held-out WRITERS
    print(f"train slots={len(Xtr)} ({len(train_docs)} actas) | "
          f"test-clean slots={len(Xte)} ({len(test_docs)} held-out actas)")

    model = ConvAE()
    opt = torch.optim.Adam(model.parameters(), 1e-3)
    lossf = nn.MSELoss()
    Xt = torch.tensor(Xtr).unsqueeze(1)
    bs = 128
    for ep in range(40):
        model.train()
        perm = torch.randperm(len(Xt))
        tot = 0.0
        for i in range(0, len(Xt), bs):
            xb = Xt[perm[i:i + bs]]
            xb = augment(xb)
            opt.zero_grad()
            loss = lossf(model(xb), xb)
            loss.backward(); opt.step()
            tot += loss.item() * len(xb)
        if ep % 10 == 9:
            print(f"  epoch {ep+1}: train mse {tot/len(Xt):.5f}")

    mean_c, max_c = errors(model, Xte)  # held-out clean

    # writer-relative FALSE-POSITIVE rate: across held-out clean actas, how often
    # does a clean form contain a digit at z>=2 (the level 131 reached)?
    zmax = []
    for d in test_docs:
        arr = np.array(by_doc[d])
        if len(arr) < 5:
            continue
        _, ml_d = errors(model, arr)
        zmax.append((ml_d.max() - np.median(ml_d)) / (ml_d.std() + 1e-9))
    zmax = np.array(zmax)
    for thr in (1.5, 2.0, 2.5, 3.0):
        print(f"  clean actas with a digit at z>={thr}: {100*(zmax>=thr).mean():4.1f}%")

    # anomaly probes (real pipeline crops, same normalization)
    probes = {}
    p52 = slot_from("data/_cvprobe/results/results.sqlite", "E14_PRE_16_001_017_01_002_delegados", 4, 1)
    p131 = slot_from("data/_cvprobe/results/results.sqlite", "E14_PRE_01_001_013_04_015_delegados", 4, 0)
    if p52 is not None: probes["52 (5-on-dot)"] = p52
    if p131 is not None: probes["131 (1-on-dot)"] = p131
    # structural hand-cuts (scale caveat): slashed-0 and struck
    for name, f, idx in [("O30 slashed-0", "files/bad/image copy 2.png", 0),
                         ("XX8 struck", "files/bad/image.png", 0)]:
        try:
            bins = slot_binaries(f); n = norm_slot(bins[idx])
            if n is not None: probes[name] = n
        except Exception:
            pass

    print("\n=== held-out CLEAN error spread (generalization baseline) ===")
    for nm, d in [("mean", mean_c), ("maxloc", max_c)]:
        print(f"  {nm}: median={np.median(d):.4f} p90={np.quantile(d,0.9):.4f} "
              f"p99={np.quantile(d,0.99):.4f} max={d.max():.4f}")
    print("\n=== anomaly probes (percentile vs held-out clean; want HIGH) ===")
    for name, img in probes.items():
        me, ml = errors(model, img[None])
        print(f"  {name:16} mean={me[0]:.4f} (p{pct(me[0],mean_c):4.1f})   "
              f"maxloc={ml[0]:.4f} (p{pct(ml[0],max_c):4.1f})")

    # WRITER-RELATIVE test: is the anomaly an outlier among ITS OWN acta's digits?
    print("\n=== writer-relative: anomaly rank within its own acta ===")
    probe_db = "data/_cvprobe/results/results.sqlite"
    con = sqlite3.connect(probe_db); con.row_factory = sqlite3.Row
    for doc, arow, aslot, label in [
        ("E14_PRE_16_001_017_01_002_delegados", 4, 1, "52 (5-on-dot)"),
        ("E14_PRE_01_001_013_04_015_delegados", 4, 0, "131 (1-on-dot)"),
    ]:
        imgs, tags = [], []
        for r in con.execute("SELECT row_number,raw_crop_path FROM vote_fields "
                             "WHERE document_id=? AND row_type='candidate' ORDER BY row_number", (doc,)):
            try:
                bins = slot_binaries(r["raw_crop_path"])
            except Exception:
                continue
            for i, bb in enumerate(bins):
                if classify_slot(extract_slot_features(bb)).slot_class != SlotClass.DIGIT:
                    continue
                n = norm_slot(bb)
                if n is None:
                    continue
                imgs.append(n); tags.append((r["row_number"], i))
        if not imgs:
            continue
        _, ml = errors(model, np.stack(imgs))
        med, sd = np.median(ml), ml.std() + 1e-9
        order = np.argsort(-ml)
        ai = tags.index((arow, aslot)) if (arow, aslot) in tags else None
        z = (ml[ai] - med) / sd if ai is not None else float("nan")
        rank = list(order).index(ai) + 1 if ai is not None else -1
        print(f"  {label:16} {len(imgs)} digit-slots in acta | anomaly maxloc={ml[ai]:.4f} "
              f"z={z:+.2f} rank={rank}/{len(imgs)} (1=highest)")
    con.close()


if __name__ == "__main__":
    main()
