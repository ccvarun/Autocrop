"""
bench.py  –  run parchi detection on a folder and print summary counts.

    python bench.py <folder>                 print counts
    python bench.py <folder> --csv out.csv   also write per-photo detail
    python bench.py <folder> --diag          dump score components for review-bucket photos

Output lines:
    cropped          N   audit failures: M
    close-up         N
    review (work)    N
    not-detected     N
"""

import csv
import sys
from pathlib import Path

import numpy as np

import parchi


def _score_components(bgr):
    """Return the scoring breakdown that detect() normally hides."""
    h, w = bgr.shape[:2]
    scale = parchi.WORK_SIZE / float(max(h, w))
    if scale < 1.0:
        work = parchi.cv2.resize(bgr, (int(w * scale), int(h * scale)),
                                 interpolation=parchi.cv2.INTER_AREA)
    else:
        scale, work = 1.0, bgr

    ctx = parchi._context(work)
    frame_area = work.shape[0] * work.shape[1]

    best, best_score, best_detail = None, 0.0, {}
    scored = []
    scored_for_enclosing = []
    import modelcand
    strategies = (("edge", parchi.candidates_from_edges(ctx["gray"], frame_area), 1.0),
                  ("paper", parchi.candidates_from_paper(work, frame_area), 0.97),
                  ("shadow", parchi.candidates_from_shadow(ctx["gray"], frame_area), 1.0),
                  ("ink", parchi.candidates_from_ink(ctx["gray"], frame_area), 0.5),
                  ("model", modelcand.candidates_from_model(work, frame_area), 1.0))

    for label, quads, weight in strategies:
        for quad in quads:
            shape = parchi.shape_score(quad, ctx["shape"])
            if shape <= 0:
                continue
            support = parchi._edge_support(ctx, quad)
            contrast = parchi._inside_outside(ctx, quad)
            evidence = 0.62 * support + 0.38 * contrast
            raw = shape * (0.18 + 0.82 * evidence)

            leak = parchi._paper_left_outside(ctx, quad)
            leak_factor = min(1.0, max(0.0, 1.0 - max(0.0, leak - 0.08) / 0.25))
            total_before_cap = raw * (0.45 + 0.55 * leak_factor)

            # Frame-edge cap
            h2, w2 = ctx["shape"][:2]
            pad = 0.012 * max(h2, w2)
            q = parchi.order_corners(quad)
            touching = sum((q[:, 0].min() <= pad, q[:, 1].min() <= pad,
                            q[:, 0].max() >= w2 - pad, q[:, 1].max() >= h2 - pad))
            if touching >= 2:
                total = min(total_before_cap, 0.50)
            elif touching == 1:
                total = min(total_before_cap, 0.80)
            else:
                total = total_before_cap

            total *= weight
            scored.append((total, quad))
            if label in ("edge", "paper"):
                scored_for_enclosing.append((total, quad))

            if total > best_score:
                best, best_score = quad, total
                best_detail = {
                    "strategy": label,
                    "shape": shape,
                    "edge_support": support,
                    "contrast": contrast,
                    "evidence": evidence,
                    "raw": raw,
                    "leak": leak,
                    "leak_factor": leak_factor,
                    "total_before_cap": total_before_cap,
                    "touching": touching,
                    "weight": weight,
                    "total": total,
                }

    if best is not None:
        encl = parchi._prefer_enclosing(best, best_score, scored_for_enclosing)
        if not np.array_equal(encl, best):
            best_detail["enclosing_applied"] = True

    return best_score, best_detail


def run(folder, csv_path=None, diag=False):
    folder = Path(folder)
    if not folder.is_dir():
        print("Not a folder: {}".format(folder))
        sys.exit(1)

    images = sorted(p for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() in parchi.EXTS)
    if not images:
        print("No images in {}".format(folder))
        sys.exit(1)

    rows = []
    buckets = {"1_cropped": [], "2_review": [], "3_not_detected": []}
    closeups = []
    audit_bad = []

    for i, path in enumerate(images):
        bgr = parchi.load(path)
        quad, score = parchi.detect(bgr)
        bucket, note = parchi.decide(bgr, quad, score, 0.62, 0.35)

        # Score breakdown
        _, detail = _score_components(bgr)

        buckets[bucket].append(path.name)
        if note == "looks like a close-up":
            closeups.append(path.name)

        # Audit confident crops
        audit_leak = 0.0
        if bucket == "1_cropped":
            audit_leak = parchi.audit_crop(bgr, quad)
            if audit_leak > 0.12:
                audit_bad.append(path.name)

        row = {
            "file": path.name,
            "bucket": bucket,
            "note": note,
            "score": round(score * 100, 1),
            "strategy": detail.get("strategy", ""),
            "shape": round(detail.get("shape", 0) * 100, 1),
            "edge_support": round(detail.get("edge_support", 0) * 100, 1),
            "contrast": round(detail.get("contrast", 0) * 100, 1),
            "evidence": round(detail.get("evidence", 0) * 100, 1),
            "raw": round(detail.get("raw", 0) * 100, 1),
            "leak": round(detail.get("leak", 0) * 100, 1),
            "leak_factor": round(detail.get("leak_factor", 0) * 100, 1),
            "total_before_cap": round(detail.get("total_before_cap", 0) * 100, 1),
            "touching": detail.get("touching", 0),
            "weight": detail.get("weight", 0),
            "audit_leak": round(audit_leak * 100, 1),
        }
        rows.append(row)

        sys.stdout.write("\r  {}/{}  {}".format(i + 1, len(images), path.name[:40]))
        sys.stdout.flush()

    print()

    # Counts
    n_cropped = len(buckets["1_cropped"])
    n_closeup = len(closeups)
    # review that are NOT close-ups = actual work for a person
    review_work = [n for n in buckets["2_review"] if n not in closeups]
    not_det_work = [n for n in buckets["3_not_detected"] if n not in closeups]
    n_review = len(review_work)
    n_not_det = len(not_det_work)

    print()
    print("  cropped          {:>3d}   audit failures: {}".format(
        n_cropped, len(audit_bad)))
    print("  close-up         {:>3d}".format(n_closeup))
    print("  review (work)    {:>3d}".format(n_review))
    print("  not-detected     {:>3d}".format(n_not_det))
    print("  total            {:>3d}".format(len(images)))

    if audit_bad:
        print()
        print("  audit failures:")
        for name in audit_bad:
            print("    {}".format(name))

    if csv_path:
        csv_path = Path(csv_path)
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print("\n  CSV written to {}".format(csv_path))

    if diag:
        # Score component summary for review-bucket photos
        review_rows = [r for r in rows if r["bucket"] == "2_review" and r["note"] != "looks like a close-up"]
        if review_rows:
            print("\n  --- review-bucket score breakdown (not close-ups) ---")
            print("  {:>40s}  {:>5s} {:>5s} {:>5s} {:>5s} {:>5s} {:>5s} {:>5s} {:>5s} {:>3s} {:>4s}".format(
                "file", "score", "shape", "edge", "cntr", "raw", "leak%", "lk_f", "b4cap", "tch", "gen"))
            for r in sorted(review_rows, key=lambda x: -x["score"]):
                print("  {:>40s}  {:>5.1f} {:>5.1f} {:>5.1f} {:>5.1f} {:>5.1f} {:>5.1f} {:>5.1f} {:>5.1f} {:>3d} {:>4s}".format(
                    r["file"][:40], r["score"], r["shape"], r["edge_support"],
                    r["contrast"], r["raw"], r["leak"], r["leak_factor"],
                    r["total_before_cap"], r["touching"], r["strategy"][:4]))

    return rows, audit_bad


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or "-h" in args or "--help" in args:
        print(__doc__)
        sys.exit(0)

    folder = args[0]
    csv_out = None
    diag = False
    i = 1
    while i < len(args):
        if args[i] == "--csv" and i + 1 < len(args):
            csv_out = args[i + 1]
            i += 2
        elif args[i] == "--diag":
            diag = True
            i += 1
        else:
            print("Unknown argument: {}".format(args[i]))
            sys.exit(1)
    run(folder, csv_out, diag)
