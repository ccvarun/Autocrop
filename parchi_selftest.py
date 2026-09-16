"""
parchi_selftest.py : make fake parchi photos, run detection, check the results.

    python parchi_selftest.py [--keep]

Real photos from the centres are the only thing that can tell us the true hit
rate.  This suite exists for the other question, which matters just as much:
when parchi says it is confident, is it actually right?  A confident crop with
bad corners is the one failure that loses money off a bill, so that is what is
asserted here.  Detection rate is only reported, not asserted, on the hard sets.

--keep leaves the generated photos in place so you can look at them.
"""

import csv
import filecmp
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

import parchi

HERE = Path(__file__).resolve().parent
rng = np.random.default_rng(20260908)

passed = failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  ok    {}".format(name))
    else:
        failed += 1
        print("  FAIL  {}  {}".format(name, detail))


# --------------------------------------------------------------------------
# fake photographs
# --------------------------------------------------------------------------

def make_slip(w=800, h=1100):
    """A handwritten bill: cream paper, ruled lines, blue scrawl, a stamp."""
    slip = np.full((h, w, 3), 246, np.uint8)
    slip[:, :, 0] = 238                                    # slightly warm paper
    cv2.rectangle(slip, (0, 0), (w - 1, h - 1), (205, 205, 205), 3)
    cv2.rectangle(slip, (40, 40), (w - 40, 150), (120, 120, 120), 2)
    for y in range(220, h - 120, 90):                      # ruled lines
        cv2.line(slip, (50, y), (w - 50, y), (200, 200, 195), 2)
        for x in range(70, w - 120, 60):                   # handwriting
            if rng.random() < 0.75:
                pts = np.array([[x + int(rng.integers(-8, 8)),
                                 y - int(rng.integers(5, 40))] for _ in range(4)])
                cv2.polylines(slip, [pts.reshape(-1, 1, 2)], False,
                              (150, 60, 40), 3, cv2.LINE_AA)
    cv2.circle(slip, (w - 150, h - 150), 90, (90, 60, 160), 4)
    return slip


def make_background(kind, w, h):
    if kind == "wood":
        bg = np.zeros((h, w, 3), np.uint8)
        bg[:, :] = (55, 90, 135)
        for x in range(0, w, 7):
            bg[:, x:x + 3] = np.clip(bg[:, x:x + 3].astype(int) + rng.integers(-25, 25), 0, 255)
    elif kind == "cloth":
        bg = np.full((h, w, 3), 45, np.uint8)
        bg = np.clip(bg.astype(int) + rng.integers(-18, 18, (h, w, 3)), 0, 255).astype(np.uint8)
    elif kind == "white_table":
        bg = np.full((h, w, 3), 238, np.uint8)          # the hard one
        bg = np.clip(bg.astype(int) + rng.integers(-6, 6, (h, w, 3)), 0, 255).astype(np.uint8)
    elif kind == "busy":
        bg = np.full((h, w, 3), 205, np.uint8)
        for _ in range(400):
            x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
            cv2.rectangle(bg, (x, y), (x + int(rng.integers(10, 60)), y + 6),
                          (70, 70, 70), -1)
    else:
        bg = np.full((h, w, 3), 120, np.uint8)
    return bg


def photograph(slip, bg_kind, out_path, fill=0.4, tilt=14.0, shadow=True, blur=1.4):
    """Composite the slip into a background under a random perspective."""
    H, W = 1600, 1200
    canvas = make_background(bg_kind, W, H)
    sh, sw = slip.shape[:2]

    scale = np.sqrt(fill * W * H / (sw * sh))
    cw, ch = sw * scale, sh * scale
    cx, cy = W / 2 + rng.integers(-60, 60), H / 2 + rng.integers(-60, 60)

    base = np.array([[cx - cw / 2, cy - ch / 2], [cx + cw / 2, cy - ch / 2],
                     [cx + cw / 2, cy + ch / 2], [cx - cw / 2, cy + ch / 2]], np.float32)
    jitter = rng.uniform(-tilt, tilt, (4, 2)) * (min(cw, ch) / 100.0)
    corners = (base + jitter).astype(np.float32)

    src = np.array([[0, 0], [sw, 0], [sw, sh], [0, sh]], np.float32)
    matrix = cv2.getPerspectiveTransform(src, corners)
    warped = cv2.warpPerspective(slip, matrix, (W, H))
    mask = cv2.warpPerspective(np.full((sh, sw), 255, np.uint8), matrix, (W, H))

    if shadow:                                   # soft drop shadow under the paper
        shadow_mask = cv2.GaussianBlur(np.roll(mask, (14, 14), (0, 1)), (61, 61), 0)
        canvas = (canvas * (1 - 0.45 * shadow_mask[..., None] / 255.0)).astype(np.uint8)

    photo = np.where(mask[..., None] > 0, warped, canvas)

    ys, xs = np.mgrid[0:H, 0:W]                  # uneven room lighting
    light = 0.72 + 0.4 * (1 - ((xs - W * 0.3) ** 2 + (ys - H * 0.25) ** 2) / float(W * H))
    photo = np.clip(photo * light[..., None], 0, 255).astype(np.uint8)

    photo = cv2.GaussianBlur(photo, (0, 0), blur)
    photo = np.clip(photo.astype(int) + rng.normal(0, 4, photo.shape), 0, 255).astype(np.uint8)
    cv2.imwrite(str(out_path), photo, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return corners


def build_set(folder):
    """Returns {filename: true corners}."""
    folder.mkdir(parents=True, exist_ok=True)
    truth = {}
    slip = make_slip()
    easy = ["wood", "cloth", "busy"]
    for i, bg in enumerate(easy * 3):
        name = "easy_{:02d}_{}.jpg".format(i, bg)
        truth[name] = photograph(slip, bg, folder / name,
                                 fill=float(rng.uniform(0.25, 0.55)),
                                 tilt=float(rng.uniform(4, 16)))
    for i in range(4):                            # white paper on a white table
        name = "hard_white_{:02d}.jpg".format(i)
        truth[name] = photograph(slip, "white_table", folder / name,
                                 fill=float(rng.uniform(0.3, 0.5)), tilt=10.0)
    for i in range(2):                            # paper nearly filling the frame
        name = "hard_full_{:02d}.jpg".format(i)
        truth[name] = photograph(slip, "wood", folder / name, fill=0.93, tilt=3.0)

    # A close-up with no background at all: the photographer filled the
    # viewfinder with the slip, which is finished work, not a failure.
    for i in range(2):
        name = "closeup_{:02d}.jpg".format(i)
        crop = slip[60 + i * 20:-60, 40:-40]
        cv2.imwrite(str(folder / name),
                    cv2.resize(crop, (1200, 1600)), [cv2.IMWRITE_JPEG_QUALITY, 85])
        truth[name] = np.array([[0, 0], [1200, 0], [1200, 1600], [0, 1600]],
                               dtype="float32")
    return truth


def corner_error(detected, truth, diagonal):
    """Mean corner distance as a fraction of the photo diagonal."""
    a = parchi.order_corners(detected)
    b = parchi.order_corners(truth)
    return float(np.mean(np.linalg.norm(a - b, axis=1)) / diagonal)


# --------------------------------------------------------------------------

def main():
    keep = "--keep" in sys.argv
    work = Path(tempfile.mkdtemp(prefix="parchi-selftest-"))
    photos = work / "photos"
    try:
        truth = build_set(photos)
        diagonal = float(np.hypot(1600, 1200))

        print("[detection accuracy on {} generated photos]".format(len(truth)))
        confident_errors, buckets = [], {"1_cropped": [], "2_review": [], "3_not_detected": []}
        for name, true_corners in sorted(truth.items()):
            bgr = parchi.load(photos / name)
            quad, score = parchi.detect(bgr)
            if score >= 0.62:
                bucket = "1_cropped"
            elif score >= 0.35:
                bucket = "2_review"
            else:
                bucket = "3_not_detected"
            buckets[bucket].append(name)
            if bucket == "1_cropped" and quad is not None:
                confident_errors.append((name, corner_error(quad, true_corners, diagonal)))

        worst = max(confident_errors, key=lambda x: x[1]) if confident_errors else ("none", 0)
        check("confident crops are accurate (worst corner error under 4%)",
              worst[1] < 0.04,
              "worst was {} at {:.1f}%".format(worst[0], worst[1] * 100))
        check("no confident crop is badly wrong (none over 8%)",
              all(e < 0.08 for _, e in confident_errors),
              str([(n, round(e * 100)) for n, e in confident_errors if e >= 0.08]))

        easy = [n for n in truth if n.startswith("easy_")]
        easy_hits = sum(1 for n in easy if n in buckets["1_cropped"])
        check("clear backgrounds mostly get cropped ({}/{})".format(easy_hits, len(easy)),
              easy_hits >= int(0.7 * len(easy)),
              "only {} of {}".format(easy_hits, len(easy)))

        hard = [n for n in truth if n.startswith("hard_")]
        hard_bad = [n for n in hard if n in buckets["1_cropped"]
                    and dict(confident_errors).get(n, 0) > 0.04]
        check("hard photos are never confidently mis-cropped", not hard_bad, str(hard_bad))

        # A slip photographed so close that its corners fall outside the frame
        # is an incomplete bill.  Cropping it confidently would cut away real
        # content, so it has to go to a person.
        cut_off = [n for n, c in truth.items()
                   if c[:, 0].min() < 0 or c[:, 1].min() < 0
                   or c[:, 0].max() > 1200 or c[:, 1].max() > 1600]
        check("slips cut off by the frame go to a person ({} of them)".format(len(cut_off)),
              all(n not in buckets["1_cropped"] for n in cut_off),
              str([n for n in cut_off if n in buckets["1_cropped"]]))

        # ---------- white-on-white: shadow-based detection --------------------
        # These photos have the same colour paper on the same colour surface.
        # The only signal is the soft drop shadow the sheet casts.  Detection
        # rate is only reported (real photos determine the true number), but
        # the precision invariant is always asserted: a shadow-detected crop
        # must never reach 1_cropped, because shadow quads are too imprecise.
        white = [n for n in truth if n.startswith("hard_white_")]
        white_confident = [n for n in white if n in buckets["1_cropped"]]
        white_review = [n for n in white if n in buckets["2_review"]]
        white_none = [n for n in white if n in buckets["3_not_detected"]]

        # Gather strategy labels so we can see which generator fired.
        white_strategies = {}
        for name in white:
            bgr = parchi.load(photos / name)
            _, _, detail = parchi.detect(bgr, explain=True)
            white_strategies[name] = detail.get("strategy", "none")

        shadow_fired = [n for n in white if white_strategies[n] == "shadow"]

        check("white-on-white is never confidently cropped (precision invariant)",
              not white_confident,
              "these reached 1_cropped: {}".format(white_confident))

        # Detection rate reported, not asserted.
        print("     white-on-white: review {}/{}, not-detected {}/{}, "
              "shadow fired on {}".format(
                  len(white_review), len(white),
                  len(white_none), len(white),
                  [n.replace("hard_white_", "w") for n in shadow_fired]))

        print("\n     cropped {}, review {}, not detected {}".format(
            len(buckets["1_cropped"]), len(buckets["2_review"]), len(buckets["3_not_detected"])))
        if confident_errors:
            print("     mean corner error on confident crops: {:.1f}%".format(
                100 * np.mean([e for _, e in confident_errors])))

        print("\n[close-ups: nothing to crop]")
        closeups = [n for n in truth if n.startswith("closeup_")]
        others = [n for n in truth if not n.startswith("closeup_")]
        check("a slip filling the frame is recognised as needing no crop",
              all(parchi.fills_frame(parchi.load(photos / n)) for n in closeups),
              [n for n in closeups if not parchi.fills_frame(parchi.load(photos / n))])
        check("close-ups are flagged as such",
              all(parchi.decide(parchi.load(photos / n), None, 0.0, 0.62, 0.35)[1]
                  == "looks like a close-up" for n in closeups))

        # The flag must never file a photo as finished by itself.  A white slip
        # on a white desk is indistinguishable from a close-up by every measure
        # here, so anything flagged still has to pass in front of a person.
        waved = []
        for n in truth:
            bgr = parchi.load(photos / n)
            q, sc = parchi.detect(bgr)
            bucket, note = parchi.decide(bgr, q, sc, 0.62, 0.35)
            if note and bucket == "1_cropped":
                waved.append(n)
        check("a flagged photo is never filed as finished on its own",
              not waved, waved)

        print("\n[pipeline]")
        result = subprocess.run([sys.executable, str(HERE / "parchi.py"), "--no-pause",
                                 "--debug", str(photos)],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL)
        out = photos / "_parchi"
        check("exit code 0", result.returncode == 0, result.stderr[-300:])

        produced = []
        for bucket in parchi.BUCKETS:
            if (out / bucket).is_dir():
                produced += [p for p in (out / bucket).iterdir() if p.is_file()]
        check("every photo accounted for, none lost",
              len(produced) == len(truth),
              "{} in, {} out".format(len(truth), len(produced)))

        untouched = out / "3_not_detected"
        if untouched.is_dir():
            same = all(filecmp.cmp(p, photos / p.name, shallow=False)
                       for p in untouched.iterdir() if p.is_file())
        else:
            same = True
        check("undetected originals copied byte for byte", same)

        # 2_review also copies byte for byte.  The bucket is triage: it tells a
        # person "detection found something here" via report.csv and --debug.
        # Re-encoding would make the file larger and degrade quality for no gain.
        review_out = out / "2_review"
        if review_out.is_dir():
            same_review = all(filecmp.cmp(p, photos / p.name, shallow=False)
                              for p in review_out.iterdir() if p.is_file())
        else:
            same_review = True
        check("review originals copied byte for byte", same_review)


        with open(out / "report.csv", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        check("report.csv has a row per photo", len(rows) == len(truth),
              "{} rows".format(len(rows)))
        check("report.csv carries the confidence",
              all(r["confidence_percent"] != "" for r in rows))
        check("debug overlays written",
              (out / "_debug").is_dir()
              and len(list((out / "_debug").iterdir())) == len(truth))

        print("\n[audit]")
        audited = subprocess.run([sys.executable, str(HERE / "parchi.py"), "--no-pause",
                                  "--audit", str(photos), "-o", str(work / "audited")],
                                 capture_output=True, text=True, stdin=subprocess.DEVNULL)
        check("audit runs and reports", "audit:" in audited.stdout, audited.stdout[-200:])
        check("audit passes on crops that keep the whole slip",
              "0 look like they cut into the bill" in audited.stdout,
              [l for l in audited.stdout.splitlines() if "%" in l and "outside" in l])

        # A crop that deliberately cuts the slip in half must be caught.
        one = sorted(photos.glob("easy_*.jpg"))[0]
        bgr = parchi.load(one)
        full = truth[one.name]
        half = parchi.order_corners(full).copy()
        half[2][1] = half[1][1] + (half[2][1] - half[1][1]) * 0.45
        half[3][1] = half[0][1] + (half[3][1] - half[0][1]) * 0.45
        check("audit catches a crop that cuts the slip in half",
              parchi.audit_crop(bgr, half) > 0.12,
              "leak {:.2f}".format(parchi.audit_crop(bgr, half)))
        check("audit is quiet about a correct crop",
              parchi.audit_crop(bgr, full) <= 0.12,
              "leak {:.2f}".format(parchi.audit_crop(bgr, full)))

        print("\n[the audit is a veto, not just a report]")
        import parchi as P
        check("one shared limit, not two",
              P.AUDIT_LEAK_LIMIT == 0.12 and "0.125" not in open(HERE / "parchi.py",
                                                                 encoding="utf-8").read(),
              "a second hand-fitted threshold has crept back in")

        # A crop that cuts the slip in half must never be called finished, no
        # matter how confident the score is.
        one = sorted(photos.glob("easy_*.jpg"))[0]
        bgr = parchi.load(one)
        half = parchi.order_corners(truth[one.name]).copy()
        half[2][1] = half[1][1] + (half[2][1] - half[1][1]) * 0.45
        half[3][1] = half[0][1] + (half[3][1] - half[0][1]) * 0.45
        bucket, note = parchi.decide(bgr, half, 0.99, 0.62, 0.35)
        check("a cutting crop is refused even at 99% confidence",
              bucket == "2_review" and "cut" in note, (bucket, note))
        good = parchi.order_corners(truth[one.name])
        check("a correct crop at the same confidence is kept",
              parchi.decide(bgr, good, 0.99, 0.62, 0.35)[0] == "1_cropped")

        print("\n[pdf]")
        import pdfout
        made = subprocess.run([sys.executable, str(HERE / "parchi.py"), "--no-pause",
                               "--pdf", str(photos), "-o", str(work / "pdfrun")],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)
        pdf = work / "pdfrun" / "all_pages.pdf"
        check("a pdf is written", pdf.exists(), made.stdout[-200:])
        check("it is a real pdf", pdf.exists() and pdf.read_bytes()[:5] == b"%PDF-")
        pages = sorted((work / "pdfrun" / "1_cropped").glob("*.jpg"))
        check("every finished page is in it",
              pdf.exists() and pdf.read_bytes().count(b"/Type /Page") >= len(pages),
              "{} pages on disk".format(len(pages)))
        check("pages are embedded, not re-encoded",
              pdf.exists() and pdf.stat().st_size >= 0.9 * sum(p.stat().st_size for p in pages),
              "pdf {} vs jpegs {}".format(pdf.stat().st_size,
                                          sum(p.stat().st_size for p in pages)))
        nothing, why = pdfout.build_pdf([], work / "empty.pdf")
        check("an empty run says so instead of writing a broken file",
              nothing is None and "no pages" in why, why)
        bad, why = pdfout.build_pdf(pages, work / "bad.pdf", page_size="postcard")
        check("an unknown page size is refused", bad is None and "unknown" in why, why)

        print("\n[safety and input handling]")
        one = sorted(photos.glob("easy_*.jpg"))[0]
        rotated = work / "rotated.jpg"
        im = Image.open(one)
        exif = im.getexif()
        exif[274] = 6
        im.save(rotated, exif=exif)
        r = subprocess.run([sys.executable, str(HERE / "parchi.py"), "--no-pause",
                            str(rotated), "-o", str(work / "rot")],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        check("EXIF rotated photo handled", r.returncode == 0, r.stderr[-200:])

        before = sorted(p.name for p in photos.iterdir() if p.is_file())
        subprocess.run([sys.executable, str(HERE / "parchi.py"), "--no-pause", str(photos)],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
        after = sorted(p.name for p in photos.iterdir() if p.is_file())
        check("originals never modified or removed", before == after)

        rerun = subprocess.run([sys.executable, str(HERE / "parchi.py"), "--no-pause",
                                str(photos)], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL)
        processed = rerun.stdout.count(" ... ")
        check("re-running does not eat its own output",
              processed == len(truth),
              "processed {} files on re-run, expected {}".format(processed, len(truth)))

        bad = subprocess.run([sys.executable, str(HERE / "parchi.py"), "--no-pause",
                              str(photos), "--confidence"],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL)
        check("missing option value is a clean error",
              bad.returncode == 2 and "needs a value" in bad.stdout and not bad.stderr.strip())
        bad = subprocess.run([sys.executable, str(HERE / "parchi.py"), "--no-pause",
                              str(photos), "--review", "90", "--confidence", "50"],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL)
        check("contradictory thresholds rejected",
              bad.returncode == 2 and "cannot be higher" in bad.stdout)

        print("\n{} passed, {} failed.".format(passed, failed))
        if keep:
            kept = HERE / "_selftest_photos"
            shutil.rmtree(kept, ignore_errors=True)
            shutil.copytree(work, kept)
            print("photos kept in {}".format(kept))
        return 1 if failed else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
