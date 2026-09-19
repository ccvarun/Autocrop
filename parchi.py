"""
parchi : find the paper in a phone photo, straighten it, crop it.

Built for handwritten bills photographed by many different people in many
different places, so it assumes nothing about background, angle or lighting.

    parchi.exe <folders or files...> [options]

Every photo lands in one of three folders and NOTHING is ever destroyed:

    _parchi/1_cropped         paper found confidently, straightened and cropped
    _parchi/2_review          something found but not trusted, original untouched
    _parchi/3_not_detected    original copied through, untouched
    _parchi/report.csv        one row per photo, with the confidence score
    _parchi/_debug            detection drawn on the photo, with --debug

The three folders exist because these are bills.  A crop that slices off an
amount is far worse than no crop, so anything doubtful is handed to a person
instead of being guessed at.
"""

import csv
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from pdfout import build_pdf
import modelcand

__version__ = "1.3"

EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
OUTDIR_NAME = "_parchi"
SMALLER_NAME = "_smaller"

# What "make it smaller" means, unless somebody says otherwise.  Measured on 26
# real parchis: the originals are 61.5 MB, and at 2000px/q90 they are 21.4 MB
# with the pen and the Devanagari still legible at 1:1.  Going to 1600 or to
# q85 saves more and starts to show; going to 2400 costs 8 MB for a difference
# nobody can see at the size these are read.
FIT_LONGEST = 2000
FIT_QUALITY = 90

# How much of the paper a finished crop may leave outside itself before it
# counts as cutting into the bill.  Used in exactly two places: the veto in
# decide(), and the --audit report.  They must be the same number, or the tool
# can ship a crop that its own audit rejects.
AUDIT_LEAK_LIMIT = 0.12
BUCKETS = ("1_cropped", "2_review", "3_not_detected")

# Detection runs on a downscaled copy: faster, and less confused by grain.
WORK_SIZE = 1200

USAGE = """parchi {v} : straighten and crop photographed paper slips

  Drag folders or image files onto parchi.exe, or:
    parchi.exe <folders or files...> [options]

  Options
    -c, --confidence N   how sure detection must be to crop outright.
                         0-100, default 62.  Lower it and more photos get
                         cropped but more get cropped wrongly.
    -r, --review N       below this, the photo is left untouched instead of
                         loosely cropped.  0-100, default 35.
    -m, --margin N       percent of extra paper left around a confident crop.
                         Default 1.
    -b, --brightness N   lighten or darken the finished crop, -80 to 80.
                         Default 0.
        --contrast N     contrast on the finished crop, 50 to 200 percent.
                         Default 100.  Pivots on mid grey, so raising it does
                         not also shift the brightness.
    -e, --enhance        lift contrast so faint pen is easier to read.
                         Applies to cropped output only: the review and
                         not-detected folders hold untouched originals.
    -o, --out DIR        where the three folders go.  Default: a "_parchi"
                         folder beside the photos.
        --smaller        do nothing but make the photos smaller.  No
                         detection, no cropping: every photo is written,
                         resized, into a "_smaller" folder beside them.
        --fit N          longest side of every saved photo, in pixels.
                         Default {fit}.  0 leaves the size alone.
    -q, --quality N      JPEG quality of every saved photo, 40 to 100.
                         Default {q}.
        --full-size      same as --fit 0: save at the original size.
    -d, --debug          also write the detected outline drawn on each photo.
        --pdf            also put every finished page into one PDF, in file
                         name order, beside the output folders.
        --a4             the same, with every page fitted to A4 for printing.
    -a, --audit          after cropping, check every crop for paper left
                         outside it and name the ones worth looking at.
                         Exits with an error if any crop looks like it cut
                         into the bill.
        --redo           start fresh: ignore decided.csv and crop every
                         photo again, replacing any work done by hand.
        --no-pause       do not wait for a keypress at the end.
    -h, --help           this text.

  Output
    _parchi/1_cropped        cropped and straightened
    _parchi/2_review         found something, not sure, loose crop
    _parchi/3_not_detected   original copied through untouched
    _parchi/report.csv       every photo with its score, for checking
    _smaller/                with --smaller: resized copies, nothing else
    _parchi/decided.csv      photos a person has already settled.  A second
                             run on the same folder leaves these alone, so
                             hand corrections survive.  Delete it, or pass
                             --redo, to start over.

  Originals are never modified.
""".format(v=__version__, fit=FIT_LONGEST, q=FIT_QUALITY)


class ArgError(Exception):
    pass


class HelpWanted(Exception):
    pass


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

def order_corners(pts):
    """Return the four corners as top-left, top-right, bottom-right, bottom-left."""
    pts = np.array(pts, dtype="float32").reshape(4, 2)
    total = pts.sum(axis=1)
    diff = (pts[:, 1] - pts[:, 0])
    return np.array([pts[np.argmin(total)],   # top left
                     pts[np.argmin(diff)],    # top right
                     pts[np.argmax(total)],   # bottom right
                     pts[np.argmax(diff)]],   # bottom left
                    dtype="float32")


def side_lengths(q):
    return [float(np.linalg.norm(q[(i + 1) % 4] - q[i])) for i in range(4)]


def shape_score(quad, shape):
    """0 to 1.  How much this quadrilateral looks like a sheet of paper.

    Shape alone cannot tell a correct detection from a wrong one: the outline
    of the whole photo is also a perfect rectangle.  This is only half the
    score, the other half is evidence from the pixels.
    """
    h, w = shape[:2]
    quad = order_corners(quad)
    area = abs(cv2.contourArea(quad))
    if area <= 0:
        return 0.0

    fill = area / float(h * w)
    # A slip that a person photographed deliberately fills a real share of the
    # frame.  Anything tiny is usually a box printed on the slip, not the slip.
    if fill < 0.10 or fill > 0.985:
        return 0.0
    fill_score = min(1.0, fill / 0.50)

    worst_angle = 0.0
    for i in range(4):
        a = quad[(i - 1) % 4] - quad[i]
        b = quad[(i + 1) % 4] - quad[i]
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na < 1e-3 or nb < 1e-3:
            return 0.0
        cosv = float(np.dot(a, b) / (na * nb))
        worst_angle = max(worst_angle, abs(np.degrees(np.arccos(np.clip(cosv, -1, 1))) - 90))
    angle_score = max(0.0, 1.0 - worst_angle / 40.0)

    top, right, bottom, left = side_lengths(quad)
    if min(top, right, bottom, left) < 20:
        return 0.0
    pair_score = min(min(top, bottom) / max(top, bottom),
                     min(left, right) / max(left, right))

    long_side = max(top + bottom, left + right) / 2.0
    short_side = min(top + bottom, left + right) / 2.0
    ratio = long_side / max(short_side, 1.0)
    if ratio > 5.0:                       # a header strip, a ruled line, a table row
        return 0.0
    ratio_score = 1.0 if ratio <= 2.5 else max(0.0, 1.0 - (ratio - 2.5) / 3.5)

    pad = 0.012 * max(h, w)
    touching = sum((quad[:, 0].min() <= pad, quad[:, 1].min() <= pad,
                    quad[:, 0].max() >= w - pad, quad[:, 1].max() >= h - pad))
    edge_score = {0: 1.0, 1: 0.88, 2: 0.45, 3: 0.15, 4: 0.02}[touching]

    return float(fill_score * 0.20 + angle_score * 0.25 + pair_score * 0.22
                 + ratio_score * 0.10 + edge_score * 0.23)


def _edge_support(ctx, quad):
    """Fraction of the outline that actually lies on a brightness edge.

    A real paper boundary shows up as a gradient all the way round.  The
    outline of the photo itself, or a box drawn around some handwriting,
    does not.  The weakest of the four sides counts double, because three
    good sides and one guessed side is exactly how a bill loses its total.
    """
    peak = ctx["gradient_peak"]
    mag = ctx["gradient_max"]
    h, w = mag.shape[:2]
    quad = order_corners(quad)
    per_side = []
    for i in range(4):
        a, b = quad[i], quad[(i + 1) % 4]
        steps = int(np.clip(np.linalg.norm(b - a) / 4.0, 12, 60))
        hits = 0
        for t in np.linspace(0.06, 0.94, steps):       # skip the corners
            x = int(round(a[0] + (b[0] - a[0]) * t))
            y = int(round(a[1] + (b[1] - a[1]) * t))
            if 0 <= x < w and 0 <= y < h and mag[y, x] > 0.12 * peak:
                hits += 1
        per_side.append(hits / float(steps))
    per_side.sort()
    return float(0.4 * per_side[0] + 0.6 * (sum(per_side) / 4.0))


def _inside_outside(ctx, quad):
    """Is what is inside the outline actually paper sitting on something else?

    Compares brightness and colourfulness just inside the outline against a
    ring just outside it.  Near zero difference means the outline is drawn
    across one continuous surface, which is not a piece of paper.
    """
    h, w = ctx["value"].shape[:2]
    poly = order_corners(quad).astype(np.int32)
    filled = np.zeros((h, w), np.uint8)
    cv2.fillPoly(filled, [poly], 255)
    band = max(4, int(0.012 * max(h, w)))
    kernel = np.ones((band * 2 + 1, band * 2 + 1), np.uint8)
    inside = cv2.erode(filled, kernel)
    outside = cv2.subtract(cv2.dilate(filled, kernel), filled)
    if inside.sum() < 255 * 50 or outside.sum() < 255 * 50:
        return 0.0

    value, sat = ctx["value"], ctx["sat"]
    v_in = float(cv2.mean(value, inside)[0])
    v_out = float(cv2.mean(value, outside)[0])
    s_in = float(cv2.mean(sat, inside)[0])
    s_out = float(cv2.mean(sat, outside)[0])
    brightness = min(1.0, abs(v_in - v_out) / 35.0)
    colourfulness = min(1.0, abs(s_in - s_out) / 30.0)
    paper_side = 1.0 if v_in >= v_out else 0.75     # paper is usually the bright side
    return float(paper_side * max(brightness, 0.7 * colourfulness))


def _paper_left_outside(ctx, quad):
    """Fraction of the paper in this photo that falls outside the outline.

    A receipt's block of printed text is a cleaner rectangle than the torn,
    shadowed edge of the paper around it, so a detector will happily lock onto
    the text and cut the last few lines off the bill.  Asking how much paper is
    still outside catches that: the sheet itself leaves almost none.
    """
    total = ctx.get("paper_total", 0.0)
    if total < 500:
        return 0.0
    mask = np.zeros(ctx["paper"].shape, np.uint8)
    cv2.fillPoly(mask, [order_corners(quad).astype(np.int32)], 255)
    # A little tolerance, so a crop a few pixels inside the edge is not punished.
    band = max(3, int(0.012 * max(mask.shape)))
    mask = cv2.dilate(mask, np.ones((band, band), np.uint8))
    outside = float(((ctx["paper"] > 0) & (mask == 0)).sum())
    return outside / total


def score_quad(ctx, quad):
    """Shape plausibility tempered by what the pixels actually show."""
    shape = shape_score(quad, ctx["shape"])
    if shape <= 0:
        return 0.0, 0.0, 0.0
    support = _edge_support(ctx, quad)
    contrast = _inside_outside(ctx, quad)
    evidence = 0.62 * support + 0.38 * contrast
    total = shape * (0.18 + 0.82 * evidence)

    # Leaving a lot of the sheet outside the outline is the signature of having
    # found something drawn on the bill rather than the bill.
    leak = _paper_left_outside(ctx, quad)
    leak_score = min(1.0, max(0.0, 1.0 - max(0.0, leak - 0.08) / 0.25))
    total *= 0.45 + 0.55 * leak_score
    # There is deliberately no hard cap on leak here.  One was tried, at 12.5%,
    # chosen to sit between a correct crop at 12.1% and a wrong one at 12.6%
    # in a 200-photo sample.  Two photos half a percentage point apart cannot
    # define a threshold, and it sat above the audit's own limit, so the tool
    # could ship a crop its audit rejected.  The veto in decide() does this job
    # properly, against the finished crop and against a single shared limit.

    # If the paper runs off the side of the photo we are not looking at the
    # whole bill, so cropping it confidently would cut away something real.
    # Cap the score so it goes to a person instead.
    h, w = ctx["shape"][:2]
    pad = 0.012 * max(h, w)
    q = order_corners(quad)
    touching = sum((q[:, 0].min() <= pad, q[:, 1].min() <= pad,
                    q[:, 0].max() >= w - pad, q[:, 1].max() >= h - pad))
    if touching >= 2:
        total = min(total, 0.50)
    elif touching == 1:
        total = min(total, 0.80)
    return float(total), support, contrast


# --------------------------------------------------------------------------
# candidate generation, three independent strategies
# --------------------------------------------------------------------------

def _quads_from_contours(contours, frame_area):
    quads = []
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:10]:
        area = cv2.contourArea(c)
        if area < 0.06 * frame_area:
            continue
        peri = cv2.arcLength(c, True)
        found = False
        for eps in (0.02, 0.035, 0.05):
            ap = cv2.approxPolyDP(c, eps * peri, True)
            if len(ap) == 4 and cv2.isContourConvex(ap):
                quads.append(ap.reshape(4, 2).astype("float32"))
                found = True
                break
        if not found:
            # Fall back to the tightest rotated rectangle, but only when the
            # contour actually fills it.  Otherwise it is not paper shaped.
            box = cv2.boxPoints(cv2.minAreaRect(c))
            if cv2.contourArea(box.astype(np.float32)) < 1.4 * area:
                quads.append(box.astype("float32"))
    return quads


def candidates_from_edges(gray, frame_area):
    """Works when the paper has a visible edge against the background."""
    quads = []
    blur = cv2.bilateralFilter(gray, 9, 60, 60)
    median = float(np.median(blur))
    for lo_k, hi_k in ((0.66, 1.33), (0.33, 1.0), (1.0, 2.0)):
        lo = int(max(0, lo_k * median))
        hi = int(min(255, max(lo + 10, hi_k * median)))
        edges = cv2.Canny(blur, lo, hi)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
        edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        quads.extend(_quads_from_contours(contours, frame_area))
    return quads


def candidates_from_paper(bgr, frame_area):
    """Works when the paper is brighter and greyer than what it sits on."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    sat, val = hsv[:, :, 1], hsv[:, :, 2]
    quads = []
    for sat_max, val_pct in ((70, 60), (100, 45), (60, 75)):
        threshold = max(110, int(np.percentile(val, val_pct)))
        mask = ((sat < sat_max) & (val > threshold)).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8), iterations=2)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8), iterations=2)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        quads.extend(_quads_from_contours(contours, frame_area))
    return quads


def candidates_from_ink(gray, frame_area):
    """Last resort: box whatever is written, ignoring where the paper ends.

    Deliberately weak.  It exists so that a photo with no findable paper edge
    still produces something for a human to look at in the review folder,
    never a confident crop.
    """
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    binary = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 35, 15)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8), iterations=2)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []
    biggest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(biggest) < 0.05 * frame_area:
        return []
    x, y, w, h = cv2.boundingRect(biggest)
    return [np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype="float32")]


def candidates_from_shadow(gray, frame_area):
    """Works when paper is the same colour as the surface but casts a visible drop shadow.

    A sheet of paper laid on a desk reflects more light than the surface
    directly, but the soft shadow it casts just outside its edge is darker
    than the surrounding area.  That shadow is invisible to Canny (it is a
    slow gradient, not a sharp step) but shows up clearly when the image is
    blurred at a large radius before gradient extraction.

    Because the shadow falls *outside* the paper, the detected contour is
    larger than the paper itself.  Two shrunken variants (10 % and 12 % of
    the longer image dimension) are returned so the scorer can find the one
    that best overlaps real pixel evidence.

    This generator deliberately produces low-scoring candidates.  Its scores
    stay below the confidence threshold because edge_support is near zero at
    the shadow boundary and inside/outside contrast is weak (shadow ≈ table).
    The intention is to push white-on-white photos into 2_review rather than
    3_not_detected, never to reach 1_cropped.
    """
    h_img, w_img = gray.shape[:2]
    # NOTE: σ, threshold, kernel sizes, and shrink percentages below were fitted
    # against the synthetic shadow model in parchi_selftest.py (single uniform
    # down-right offset blur); they are unvalidated against real photos.
    #
    # Blur heavily to smear the shadow gradient over its soft spatial extent
    # and suppress paper content (printed lines, handwriting) that would
    # create false edges at fine scale.
    blurred = cv2.GaussianBlur(gray.astype(np.float32), (0, 0), 20)
    gx = cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=5)
    gy = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=5)
    mag = cv2.magnitude(gx, gy)

    # Threshold at a modest fraction of the strongest gradient in the image.
    # The shadow gradient is weaker than a paper-on-wood edge, so the
    # threshold must be low enough to catch it without drowning in noise.
    thresh = 0.10 * float(np.percentile(mag, 99))
    if thresh < 1.0:
        return []
    shadow_mask = (mag > thresh).astype(np.uint8) * 255

    # Close small holes in the shadow band, then open to remove stray blobs.
    shadow_mask = cv2.morphologyEx(shadow_mask, cv2.MORPH_CLOSE,
                                   np.ones((21, 21), np.uint8))
    shadow_mask = cv2.morphologyEx(shadow_mask, cv2.MORPH_OPEN,
                                   np.ones((11, 11), np.uint8))

    contours, _ = cv2.findContours(shadow_mask, cv2.RETR_LIST,
                                   cv2.CHAIN_APPROX_SIMPLE)

    quads = []
    longest = max(w_img, h_img)
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        if cv2.contourArea(c) < 0.06 * frame_area:
            continue
        peri = cv2.arcLength(c, True)
        raw_quad = None
        for eps in (0.05, 0.08, 0.12):
            ap = cv2.approxPolyDP(c, eps * peri, True)
            if len(ap) == 4 and cv2.isContourConvex(ap):
                raw_quad = ap.reshape(4, 2).astype("float32")
                break
        if raw_quad is None:
            continue
        # Produce two inward-shifted variants.  Because the shadow lands
        # outside the paper edge, the raw contour is slightly too big.
        # Shifting toward the centroid moves the quad onto the paper boundary
        # where real pixel evidence (brightness step, ink) can be found.
        centre = raw_quad.mean(axis=0)
        directions = centre - raw_quad
        norms = np.linalg.norm(directions, axis=1, keepdims=True)
        norms = np.where(norms < 1.0, 1.0, norms)
        unit = directions / norms
        for shrink_pct in (0.10, 0.12):
            shrink_px = shrink_pct * longest
            quads.append((raw_quad + unit * shrink_px).astype("float32"))
    return quads


def _context(work):
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    gx = cv2.Sobel(blur, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(blur, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    # Local maximum, so a candidate a couple of pixels off the true edge still
    # gets credit for it.
    mag_max = cv2.dilate(mag, np.ones((7, 7), np.uint8))
    hsv = cv2.cvtColor(work, cv2.COLOR_BGR2HSV)

    # Everything that looks like paper, used to ask whether a candidate has
    # left some of the sheet outside itself.
    sat_all, val_all = hsv[:, :, 1], hsv[:, :, 2]
    threshold = max(110, int(np.percentile(val_all, 62)))
    paper = ((sat_all < 90) & (val_all > threshold)).astype(np.uint8) * 255
    paper = cv2.morphologyEx(paper, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8), iterations=2)
    paper = cv2.morphologyEx(paper, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8), iterations=2)

    return {"gray": gray,
            "paper": paper,
            "paper_total": float((paper > 0).sum()),
            "shape": work.shape,
            "gradient_max": mag_max,
            "gradient_peak": max(25.0, float(np.percentile(mag, 98))),
            "sat": hsv[:, :, 1],
            "value": hsv[:, :, 2]}


def _prefer_enclosing(best, best_score, scored):
    """Prefer a larger quadrilateral that contains the chosen one.

    Bills usually have a box or a table printed on them, and that box is a
    cleaner rectangle than the torn edge of the paper around it.  Whenever a
    nearly-as-good candidate wraps around the winner, the wrapper is the sheet
    and the winner was something drawn on it.
    """
    best_area = abs(cv2.contourArea(order_corners(best)))
    chosen = best
    chosen_area = best_area
    for total, quad in scored:
        if total < 0.72 * best_score:
            continue
        area = abs(cv2.contourArea(order_corners(quad)))
        if area <= chosen_area * 1.25:
            continue
        poly = order_corners(quad).astype(np.float32)
        inside = sum(cv2.pointPolygonTest(poly, (float(p[0]), float(p[1])), False) >= 0
                     for p in order_corners(best))
        if inside >= 3:
            chosen, chosen_area = quad, area
    return chosen


def fills_frame(bgr, limit=40.0, min_brightness=115.0):
    """True when the paper already fills the photo, so there is nothing to crop.

    People photographing a bill often fill the viewfinder with it.  There is
    then no paper boundary anywhere in the frame, detection has nothing to find,
    and the photo lands in "not detected" as though something had gone wrong.
    Nothing did: the right answer is to keep the photo as it is.

    The test is whether a thin ring around the edge of the photo looks like the
    same material as the middle, and is bright enough to be paper.  A bill lying
    on a desk fails it, because the ring is desk and the middle is paper.

    The threshold is deliberately tight.  Being wrong here means leaving a
    little background around a bill, which costs nothing; being wrong the other
    way would silently skip a photo that needed cropping.
    """
    h, w = bgr.shape[:2]
    scale = 800.0 / float(max(h, w))
    work = cv2.resize(bgr, (int(w * scale), int(h * scale)),
                      interpolation=cv2.INTER_AREA) if scale < 1.0 else bgr
    height, width = work.shape[:2]
    lab = cv2.cvtColor(work, cv2.COLOR_BGR2LAB)

    thickness = max(6, int(0.05 * min(height, width)))
    ring = np.zeros((height, width), bool)
    ring[:thickness, :] = ring[-thickness:, :] = True
    ring[:, :thickness] = ring[:, -thickness:] = True
    middle = np.zeros((height, width), bool)
    middle[height // 4:3 * height // 4, width // 4:3 * width // 4] = True

    edge = lab[ring].mean(axis=0)
    core = lab[middle].mean(axis=0)
    distance = float(np.linalg.norm(edge - core))
    return distance < limit and float(edge[0]) > min_brightness


def detect(bgr, explain=False):
    """Best quadrilateral and its score, in the coordinates of bgr."""
    h, w = bgr.shape[:2]
    scale = WORK_SIZE / float(max(h, w))
    if scale < 1.0:
        work = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    else:
        scale, work = 1.0, bgr

    ctx = _context(work)
    frame_area = work.shape[0] * work.shape[1]

    best, best_score, detail = None, 0.0, {}
    scored = []
    scored_for_enclosing = []  # only precise strategies feed _prefer_enclosing
    strategies = (("edge", candidates_from_edges(ctx["gray"], frame_area), 1.0),
                  ("paper", candidates_from_paper(work, frame_area), 0.97),
                  ("shadow", candidates_from_shadow(ctx["gray"], frame_area), 1.0),
                  ("ink", candidates_from_ink(ctx["gray"], frame_area), 0.5),
                  ("model", modelcand.candidates_from_model(work, frame_area), 1.0))
    for label, quads, weight in strategies:
        for quad in quads:
            total, support, contrast = score_quad(ctx, quad)
            total *= weight
            if total > 0:
                scored.append((total, quad))
                # Shadow and ink find approximate locations only.  Letting them
                # feed _prefer_enclosing would cause a large shadow quad to be
                # mistaken for "the sheet that contains the detected box", which
                # would replace a good precise detection with a bad one.
                if label in ("edge", "paper"):
                    scored_for_enclosing.append((total, quad))
            if total > best_score:
                best, best_score = quad, total
                detail = {"strategy": label, "edge_support": support, "contrast": contrast}

    if best is None:
        return (None, 0.0, {}) if explain else (None, 0.0)

    best = _prefer_enclosing(best, best_score, scored_for_enclosing)
    corners = order_corners(best) / scale
    return (corners, best_score, detail) if explain else (corners, best_score)


def _refine_side(mag, a, b, band):
    """Fit a straight line to the real edge lying near the segment a-b.

    Walks along the segment, and at each step looks sideways for the strongest
    brightness change within `band` pixels.  Those hits are then fitted with a
    line, throwing away the ones that disagree with the rest.  Using the whole
    side as evidence beats snapping a corner to the nearest edge pixel: one
    pixel of grain or a fold in the paper cannot drag the side with it.

    Returns (point, direction) or None when the edge is not convincing.
    """
    h, w = mag.shape[:2]
    a = np.asarray(a, dtype="float64")
    b = np.asarray(b, dtype="float64")
    length = float(np.linalg.norm(b - a))
    if length < 20:
        return None
    direction = (b - a) / length
    normal = np.array([-direction[1], direction[0]])

    samples = int(np.clip(length / 6.0, 14, 70))
    offsets = np.arange(-band, band + 1, dtype="float64")
    floor = max(6.0, 0.10 * float(np.percentile(mag, 98)))

    hits = []
    for t in np.linspace(0.10, 0.90, samples):        # corners are unreliable
        base = a + (b - a) * t
        points = base[None, :] + offsets[:, None] * normal[None, :]
        xs = np.rint(points[:, 0]).astype(int)
        ys = np.rint(points[:, 1]).astype(int)
        inside = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
        if inside.sum() < 5:
            continue
        strength = np.full(offsets.shape, -1.0)
        strength[inside] = mag[ys[inside], xs[inside]]
        best = int(np.argmax(strength))
        if strength[best] < floor:
            continue
        hits.append((offsets[best], base + offsets[best] * normal))

    if len(hits) < max(6, samples // 3):
        return None

    shifts = np.array([hit[0] for hit in hits])
    median = float(np.median(shifts))
    spread = float(np.median(np.abs(shifts - median))) or 1.0
    keep = [hit[1] for hit in hits if abs(hit[0] - median) <= 2.5 * spread]
    if len(keep) < max(5, samples // 4):
        return None

    fitted = cv2.fitLine(np.array(keep, dtype="float32"), cv2.DIST_L2, 0, 0.01, 0.01)
    vx, vy, x0, y0 = [float(v) for v in fitted.ravel()]
    if abs(vx * direction[0] + vy * direction[1]) < 0.85:      # turned a corner
        return None
    return np.array([x0, y0]), np.array([vx, vy])


def _intersect(first, second):
    (p, u), (q, v) = first, second
    denominator = u[0] * v[1] - u[1] * v[0]
    if abs(denominator) < 1e-6:                                # parallel
        return None
    t = ((q[0] - p[0]) * v[1] - (q[1] - p[1]) * v[0]) / denominator
    return p + t * u


def _snap_pass(bgr, quad, band_pct):
    """One sweep: fit each side within band_pct of the frame, rebuild corners."""
    h, w = bgr.shape[:2]
    scale = 1600.0 / float(max(h, w))
    if scale < 1.0:
        work = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    else:
        scale, work = 1.0, bgr
    small = order_corners(quad) * scale

    gray = cv2.GaussianBlur(cv2.cvtColor(work, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)

    band = max(6.0, band_pct / 100.0 * min(work.shape[:2]))
    sides = [_refine_side(mag, small[i], small[(i + 1) % 4], band) for i in range(4)]

    moved = np.array(small, dtype="float64")
    for i in range(4):
        before, after = sides[(i - 1) % 4], sides[i]
        if before is None or after is None:
            continue
        corner = _intersect(before, after)
        if corner is None:
            continue
        # Within a pass a corner may only travel as far as that pass could see.
        if np.linalg.norm(corner - small[i]) > 2.5 * band:
            continue
        moved[i] = corner
    return moved / scale


def snap_to_edges(bgr, quad, bands=(5.0, 2.0, 1.0)):
    """Pull a roughly placed quadrilateral onto the paper's actual edges.

    Each side is fitted separately and the corners come from intersecting
    neighbouring sides, so a corner lands where two edges meet even when that
    point is blurred, in shadow, or under a thumb.

    Coarse to fine.  A person dragging a corner drops it roughly, often a
    hundred pixels out, so the first sweep searches wide enough to reach the
    edge at all; later sweeps narrow down to lock onto it precisely.  A single
    narrow sweep cannot see far enough to be useful, and a single wide one
    happily grabs the edge of the table instead.

    Any side that cannot be found keeps its position, and a result that moves
    a corner absurdly far or collapses the shape is discarded: a magnet that
    drags the outline somewhere the person did not point is worse than none.
    """
    original = order_corners(quad)
    current = original.copy()
    for band_pct in bands:
        current = _snap_pass(bgr, current, band_pct)

    result = order_corners(current)
    if abs(cv2.contourArea(result.astype("float32"))) < 0.4 * abs(
            cv2.contourArea(original.astype("float32"))):
        return original, 0.0                       # collapsed, discard

    diagonal = float(np.hypot(*bgr.shape[:2]))
    shift = float(np.max(np.linalg.norm(result - original, axis=1)))
    if shift > 0.18 * diagonal:                    # flung somewhere else
        return original, 0.0
    return result, shift


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def quad_touches_frame(quad, shape, pad_pct=0.012):
    """How many of the four frame edges the outline is already up against.

    Two or more and the paper runs past the picture, so there are no four
    corners to find and no perspective to correct.  Read off the outline
    itself, with no paper mask involved, because the mask is the part of this
    tool that cannot be trusted on a patterned background (see AGENTS.md).
    """
    if quad is None:
        return 0
    height, width = shape[:2]
    q = order_corners(quad)
    pad = pad_pct * max(height, width)
    return int(sum((q[:, 0].min() <= pad, q[:, 1].min() <= pad,
                    q[:, 0].max() >= width - pad, q[:, 1].max() >= height - pad)))


def trim_to_frame(quad, shape, near=0.04):
    """An outline that takes the background off the sides that have any.

    For a photo where the paper runs past the edge of the picture there are no
    four corners to find, and the tool currently hands such a photo back
    untouched with strips of background still on it.  This is the other half
    of the job: an axis-aligned box around what detection did find, with any
    side already close to the frame pushed out TO the frame.

    Two properties make it safe to OFFER.  It is never smaller than the box
    around the detected outline, so it cannot cut anything that outline would
    not have cut.  And on a side where the paper runs off the photo it removes
    nothing at all.

    It is a starting position for a person, not an answer.  Measured on 26 real
    parchis it takes a median 30 percent of the frame off, and on four of six
    inspected by eye it also clipped a second slip, a platform ticket along the
    bottom or a coloured memo at the side.  It is offered as the outline the
    editor opens with, never applied on its own.  There is no perspective
    correction here and there cannot be: you cannot straighten a sheet whose
    corners are outside the picture.
    """
    if quad is None:
        return None
    height, width = shape[:2]
    q = order_corners(quad)
    left, top = float(q[:, 0].min()), float(q[:, 1].min())
    right, bottom = float(q[:, 0].max()), float(q[:, 1].max())

    reach = near * max(height, width)
    if left < reach:
        left = 0.0
    if top < reach:
        top = 0.0
    if right > width - reach:
        right = float(width)
    if bottom > height - reach:
        bottom = float(height)

    left, top = max(0.0, left), max(0.0, top)
    right, bottom = min(float(width), right), min(float(height), bottom)
    if right - left < 50 or bottom - top < 50:
        return None
    return np.array([[left, top], [right, top],
                     [right, bottom], [left, bottom]], dtype="float32")


def warp(bgr, quad, margin_pct):
    """Straighten the paper out of the photo."""
    quad = order_corners(quad)
    if margin_pct:
        centre = quad.mean(axis=0)
        quad = centre + (quad - centre) * (1.0 + margin_pct / 100.0)

    top, right, bottom, left = side_lengths(quad)
    width = int(round(max(top, bottom)))
    height = int(round(max(left, right)))
    width, height = max(width, 10), max(height, 10)

    target = np.array([[0, 0], [width - 1, 0],
                       [width - 1, height - 1], [0, height - 1]], dtype="float32")
    matrix = cv2.getPerspectiveTransform(quad, target)
    return cv2.warpPerspective(bgr, matrix, (width, height),
                               flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)





def turn(bgr, degrees):
    """Rotate a finished crop by a quarter turn.

    Done on the result, not on the corners: warp() normalises corner order
    before it builds the transform, so re-ordering the quad to rotate would be
    silently undone.  A quarter turn is a transpose and a flip, so this costs
    nothing and resamples nothing.
    """
    d = int(degrees) % 360
    if d == 90:
        return cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)
    if d == 180:
        return cv2.rotate(bgr, cv2.ROTATE_180)
    if d == 270:
        return cv2.rotate(bgr, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return bgr


def stand_upright(bgr):
    """Put a crop on its short edge.  Parchis are taller than they are wide."""
    height, width = bgr.shape[:2]
    return turn(bgr, 90) if width > height else bgr


def enhance_image(bgr):
    """Gentle lift so faint ballpoint is readable.  Not a black and white filter."""
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    lightness, a, b = cv2.split(lab)
    lightness = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(lightness)
    return cv2.cvtColor(cv2.merge((lightness, a, b)), cv2.COLOR_LAB2BGR)


def adjust(bgr, brightness=0, contrast=1.0):
    """Brightness and contrast, the familiar pair, pivoting on mid grey.

    Pivoting matters.  Scaling straight from zero means every contrast change
    also shifts the brightness, so the two sliders fight each other and nobody
    can get where they are going.
    """
    brightness, contrast = float(brightness), float(contrast)
    if brightness == 0.0 and abs(contrast - 1.0) < 1e-3:
        return bgr
    offset = 128.0 * (1.0 - contrast) + brightness
    # A lookup table, not convertScaleAbs.  convertScaleAbs takes the ABSOLUTE
    # value before clamping, so darkening or raising contrast sent black back
    # up towards white: at contrast 1.5 every level under 43 came out
    # inverted, which is ink and shadow on a bill.  Measured at 5% of the
    # pixels on a real parchi, up to 64 levels out.  Clipping is what a person
    # expects and what every other program does.
    table = np.clip(np.arange(256) * contrast + offset, 0, 255).astype(np.uint8)
    return cv2.LUT(bgr, table)


def finish(bgr, opts):
    """Everything done to a crop after the warp, in one place.

    The preview, the batch run, the crop endpoint and the command line tool
    all come through here, so the picture a person approves is the picture
    that gets written.  Before this existed, /api/preview skipped the contrast
    lift that /api/crop applied, and the pane labelled "what you will get" was
    quietly wrong every time that box was ticked.  Same rule as decide(): one
    place, or two paths drift apart and nobody notices for a month.
    """
    if opts.get("enhance"):
        bgr = enhance_image(bgr)
    return adjust(bgr, opts.get("brightness", 0), opts.get("contrast", 1.0))


def load(path):
    """Open through Pillow so EXIF rotation from phones is applied first."""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        return cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)


def fit(bgr, longest):
    """Bring the longest side down to `longest`, or leave it alone.

    Never enlarges.  A photo already smaller than the cap is returned as it
    is, which is what stops a second pass re-encoding the first pass's output.
    INTER_AREA because this is always a reduction, and it averages rather than
    samples: on a phone photo that quietly removes sensor noise, which is why
    a shrunk parchi often looks cleaner than the original at 1:1.
    """
    if not longest:
        return bgr
    h, w = bgr.shape[:2]
    if max(h, w) <= longest:
        return bgr
    scale = longest / float(max(h, w))
    return cv2.resize(bgr, (max(1, int(round(w * scale))),
                            max(1, int(round(h * scale)))),
                      interpolation=cv2.INTER_AREA)


def save(bgr, path, quality=92):
    suffix = path.suffix.lower()
    quality = int(max(1, min(100, quality)))
    if suffix in (".jpg", ".jpeg"):
        params = [cv2.IMWRITE_JPEG_QUALITY, quality]
    elif suffix == ".png":
        params = [cv2.IMWRITE_PNG_COMPRESSION, 6]
    else:
        path = path.with_suffix(".jpg")
        params = [cv2.IMWRITE_JPEG_QUALITY, quality]
    ok = cv2.imwrite(str(path), bgr, params)
    if not ok:
        raise IOError("could not write {}".format(path))
    return path


def draw_debug(bgr, quad, score, bucket, path):
    canvas = bgr.copy()
    colour = {"1_cropped": (0, 200, 0), "2_review": (0, 190, 255),
              "3_not_detected": (0, 0, 230)}[bucket]
    if quad is not None:
        cv2.polylines(canvas, [quad.astype(np.int32)], True, colour,
                      max(2, int(max(canvas.shape[:2]) / 400)))
    label = "{}  {:.0f}%".format(bucket, score * 100)
    cv2.putText(canvas, label, (20, 60), cv2.FONT_HERSHEY_SIMPLEX,
                max(1.0, max(canvas.shape[:2]) / 1200.0), colour, 3, cv2.LINE_AA)
    scale = 900.0 / max(canvas.shape[:2])
    if scale < 1:
        canvas = cv2.resize(canvas, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(path.with_suffix(".jpg")), canvas, [cv2.IMWRITE_JPEG_QUALITY, 80])


# --------------------------------------------------------------------------
# one photo
# --------------------------------------------------------------------------

def classify(quad, score, confidence, review):
    """Which bucket a score alone puts a photo in."""
    if quad is not None and score >= confidence:
        return "1_cropped"
    if quad is not None and score >= review:
        return "2_review"
    return "3_not_detected"


def decide(bgr, quad, score, confidence, review):
    """Bucket and note for one photo.  Returns (bucket, note).

    The one place this rule lives.  The command line tool and the review page
    both call it, so they can never drift apart.
    """
    bucket = classify(quad, score, confidence, review)

    # Last gate before a crop is called finished: measure the crop that would
    # be produced and refuse it if it leaves too much of the paper outside.
    # This is the same measurement --audit reports, against the same limit, so
    # the tool cannot ship a crop its own audit rejects.  It only ever demotes,
    # so it cannot be gamed into approving anything: the scorer decides, this
    # can only veto.
    if bucket == "1_cropped" and quad is not None:
        if audit_crop(bgr, quad) > AUDIT_LEAK_LIMIT:
            return "2_review", "crop would have cut the bill"

    # A photo whose border matches its middle is usually a close-up with no
    # background at all, so there is nothing to crop.  It is NOT filed as
    # finished on that basis: a white slip on a white desk looks exactly the
    # same to every measure available here, and waving that through would
    # silently skip a bill that still needed cropping.  It is flagged instead,
    # and the review page lets a person clear the whole group in one click.
    if bucket != "1_cropped" and fills_frame(bgr):
        return bucket, "looks like a close-up"
    return bucket, ""


# A person's decision, written down, so a later run cannot walk over it.
DECIDED_NAME = "decided.csv"


def decided_path(outroot):
    return Path(outroot) / DECIDED_NAME


def load_decided(outroot):
    """Which photos a person has already settled, and where they put them.

    Without this, running the tool twice on the same folder silently replaces
    hand-made crops with automatic ones.  That is a day of somebody's work
    gone with nothing on screen to say so, which is the exact failure this
    tool exists to prevent.  The record is a plain CSV so it can be read, and
    deleted, by anyone.
    """
    out = {}
    path = decided_path(outroot)
    if not path.exists():
        return out
    try:
        with path.open("r", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("file") or "").strip()
                if name:
                    out[name] = (row.get("bucket") or "1_cropped").strip()
    except (OSError, csv.Error):
        return {}                      # unreadable is the same as not there
    return out


def mark_decided(outroot, name, bucket):
    """Record that a person settled this photo.  Rewrites the whole file, so
    the same photo decided twice leaves one row, not two."""
    decided = load_decided(outroot)
    decided[name] = bucket
    return _write_decided(outroot, decided)


def forget_all_decided(outroot):
    """Start fresh.  Only for --redo, where every photo is being cropped again
    anyway: leaving the record behind would have it claim photos were finished
    by hand when an automatic crop had just replaced them."""
    path = decided_path(outroot)
    try:
        if path.exists():
            path.unlink()
        return True
    except OSError:
        return False


def forget_decided(outroot, name):
    """Undo takes a photo back out of the record, so a later run may touch it
    again.  Undo means undo."""
    decided = load_decided(outroot)
    if decided.pop(name, None) is None:
        return True
    return _write_decided(outroot, decided)


def _write_decided(outroot, decided):
    path = decided_path(outroot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["file", "bucket"])
            for name in sorted(decided):
                writer.writerow([name, decided[name]])
        return True
    except OSError:
        return False                   # never fail a crop over a bookkeeping file


def put_image(bgr, out_path, opts):
    """Write a picture we made, at the run's size setting."""
    return save(fit(bgr, opts.get("fit", 0)), out_path, opts.get("quality", 92))


def put_original(src, out_path, opts, bgr=None):
    """Write a photo that is passing through uncropped.

    With no size setting this is the byte-for-byte copy it always was, and
    the promise that these folders hold the untouched original still holds.
    With one, the copy is resized, because that is where most photos go: on
    26 real parchis, 25 of them.  Shrinking only the cropped ones would have
    saved 2 percent and looked like a feature.
    """
    longest = opts.get("fit", 0)
    if longest:
        if bgr is None:
            bgr = load(src)
        h, w = bgr.shape[:2]
        if max(h, w) > longest:
            return put_image(bgr, out_path, opts)
    shutil.copy2(src, out_path)
    return out_path


def smaller_root(folder):
    return Path(folder) / SMALLER_NAME


def shrink_one(path, outdir, longest=FIT_LONGEST, quality=FIT_QUALITY):
    """Write a smaller copy of one photo.  Returns (out_path, before, after).

    A photo already inside the cap is copied byte for byte rather than
    re-encoded.  Re-encoding it would lose a little quality for no saving,
    and on a second pass over the same folder that loss would compound with
    nothing on screen to say so.
    """
    before = path.stat().st_size
    outdir.mkdir(parents=True, exist_ok=True)
    bgr = load(path)
    h, w = bgr.shape[:2]
    if max(h, w) <= longest:
        out = outdir / path.name
        shutil.copy2(path, out)
        return out, before, out.stat().st_size, False
    out = save(fit(bgr, longest), outdir / path.name, quality)
    return out, before, out.stat().st_size, True


def shrink_folder(folder, longest=FIT_LONGEST, quality=FIT_QUALITY,
                  progress=None):
    """Make every photo in a folder smaller, into a _smaller folder beside them.

    The originals are not touched, not moved and not overwritten: this only
    ever writes into _smaller.  collect() already skips _parchi, and it skips
    _smaller too, so running this twice reads the originals again rather than
    shrinking the shrunk.
    """
    folder = Path(folder)
    outdir = smaller_root(folder)
    rows, before_total, after_total = [], 0, 0
    files = list(collect([str(folder)]))
    for n, f in enumerate(files, 1):
        try:
            out, before, after, resized = shrink_one(f, outdir, longest, quality)
            rows.append([f.name, before, after,
                         "resized" if resized else "already small"])
            before_total += before
            after_total += after
            note = ""
        except Exception as err:
            rows.append([f.name, 0, 0, "failed: {}".format(err)])
            note = "failed: {}".format(err)
        if progress:
            progress(n, len(files), f.name, note)
    return {"folder": str(folder), "out": str(outdir), "files": len(files),
            "before": before_total, "after": after_total, "rows": rows}


def process(path, outroot, opts):
    bgr = load(path)
    quad, score = detect(bgr)

    bucket, note = decide(bgr, quad, score, opts["confidence"], opts["review"])
    if bucket == "1_cropped" and not note:
        result = warp(bgr, quad, opts["margin"])
    else:
        result = None                           # original copied through, untouched

    outdir = outroot / bucket
    outdir.mkdir(parents=True, exist_ok=True)

    if result is None:
        out = put_original(path, outdir / path.name, opts, bgr)
    else:
        result = finish(result, opts)
        out = put_image(result, outdir / path.name, opts)

    if opts["debug"]:
        debugdir = outroot / "_debug"
        debugdir.mkdir(parents=True, exist_ok=True)
        draw_debug(bgr, quad, score, bucket, debugdir / path.name)

    return bucket, score, out, note, quad


def audit_crop(bgr, quad):
    """How much of the paper this crop left behind, 0 to 1.

    An independent check on a finished crop, run after the decision rather than
    as part of it.  Cutting the total off a bill is the one failure that must
    never ship, so it is worth measuring twice.
    """
    if quad is None:
        return 0.0
    
    h, w = bgr.shape[:2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    sat, val = hsv[:, :, 1], hsv[:, :, 2]

    # Adaptive threshold handles lighting gradients and shadows perfectly
    k = int(min(h, w) * 0.5)
    if k % 2 == 0:
        k += 1
    k = max(3, k)
    thresh = cv2.adaptiveThreshold(val, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, k, 5)

    # NOTE: this calls a printed bedsheet paper.  A smoothness gate fixes that
    # and breaks something worse; see "the paper mask" in AGENTS.md before
    # touching it.  Do not "improve" this without reading that section.
    paper = ((sat < 90) & (thresh > 0)).astype(np.uint8) * 255
    paper = cv2.morphologyEx(paper, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8), iterations=2)
    paper = cv2.morphologyEx(paper, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8), iterations=2)

    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(paper, connectivity=4)
    inside = np.zeros(paper.shape, np.uint8)
    cv2.fillPoly(inside, [order_corners(quad).astype(np.int32)], 255)
    
    inside_labels = labels[inside > 0]
    if len(inside_labels) == 0:
        return 0.0
        
    counts = np.bincount(inside_labels)
    counts[0] = 0
    if len(counts) == 1:
        return 0.0
        
    best_label = np.argmax(counts)
    paper_comp = (labels == best_label).astype(np.uint8) * 255
    
    total = float((paper_comp > 0).sum())
    if total < 500:
        return 0.0
        
    band = max(2, int(0.002 * max(paper.shape)))
    inside = cv2.dilate(inside, np.ones((band, band), np.uint8))
    outside = float(((paper_comp > 0) & (inside == 0)).sum())
    return outside / total


def collect(paths):
    seen = set()
    for arg in paths:
        p = Path(arg)
        if p.is_dir():
            candidates = sorted(f for f in p.rglob("*") if f.is_file())
        elif p.is_file():
            candidates = [p]
        else:
            print("  not found: {}".format(arg))
            continue
        for f in candidates:
            # _smaller is skipped for the same reason as _parchi: a second
            # pass must read the originals, never its own output.
            if (f.suffix.lower() not in EXTS
                    or OUTDIR_NAME in f.parts or SMALLER_NAME in f.parts):
                continue
            try:
                key = f.resolve()
            except OSError:
                key = f.absolute()
            if key not in seen:
                seen.add(key)
                yield f


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def as_int(name, raw, low, high):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ArgError("{} needs a whole number, got {!r}".format(name, raw))
    if not low <= value <= high:
        raise ArgError("{} must be between {} and {}".format(name, low, high))
    return value


def parse_args(argv):
    paths = []
    opts = {"confidence": 0.62, "review": 0.35, "margin": 1,
            "enhance": False, "brightness": 0, "contrast": 1.0,
            "fit": FIT_LONGEST, "quality": FIT_QUALITY, "smaller": False,
            "debug": False, "audit": False,
            "pdf": False, "page_size": None, "out": None, "pause": True,
            "redo": False}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("-") and arg != "/?":
            paths.append(arg)
            i += 1
            continue

        name, sep, inline = arg.partition("=")

        def value():
            if sep:
                return inline
            if i + 1 >= len(argv):
                raise ArgError("{} needs a value, for example {} 60".format(name, name))
            return argv[i + 1]

        if name in ("-h", "--help", "/?"):
            raise HelpWanted()
        elif name in ("-c", "--confidence"):
            opts["confidence"] = as_int(name, value(), 0, 100) / 100.0
            i += 0 if sep else 1
        elif name in ("-r", "--review"):
            opts["review"] = as_int(name, value(), 0, 100) / 100.0
            i += 0 if sep else 1
        elif name in ("-m", "--margin"):
            opts["margin"] = as_int(name, value(), 0, 50)
            i += 0 if sep else 1
        elif name in ("-o", "--out"):
            opts["out"] = Path(value())
            i += 0 if sep else 1
        elif name in ("-b", "--brightness"):
            opts["brightness"] = as_int(name, value(), -80, 80)
            i += 0 if sep else 1
        elif name == "--contrast":
            opts["contrast"] = as_int(name, value(), 50, 200) / 100.0
            i += 0 if sep else 1
        elif name in ("-e", "--enhance"):
            opts["enhance"] = True
        elif name in ("-d", "--debug"):
            opts["debug"] = True
        elif name in ("-a", "--audit"):
            opts["audit"] = True
        elif name == "--pdf":
            opts["pdf"] = True
        elif name == "--a4":
            opts["pdf"] = True
            opts["page_size"] = "a4"
        elif name == "--fit":
            opts["fit"] = as_int(name, value(), 0, 20000)
            i += 0 if sep else 1
        elif name in ("-q", "--quality"):
            opts["quality"] = as_int(name, value(), 40, 100)
            i += 0 if sep else 1
        elif name == "--full-size":
            opts["fit"] = 0
        elif name == "--smaller":
            opts["smaller"] = True
        elif name == "--redo":
            opts["redo"] = True
        elif name == "--no-pause":
            opts["pause"] = False
        elif name in ("-v", "--version"):
            print("parchi {}".format(__version__))
            raise SystemExit(0)
        else:
            raise ArgError("unknown option: {}".format(arg))
        i += 1

    if opts["review"] > opts["confidence"]:
        raise ArgError("--review cannot be higher than --confidence")
    return paths, opts


def mb(n):
    return "{:.1f} MB".format(n / 1e6)


def shrink_main(paths, files, opts):
    """--smaller: no detection, no buckets, just smaller copies."""
    folder = Path(paths[0])
    if not folder.is_dir():
        folder = folder.parent
    print("parchi {}   {} photos   longest side {}px, quality {}\n".format(
        __version__, len(files), opts["fit"] or "unchanged", opts["quality"]))

    def say(n, total, name, note):
        print("  [{}/{}] {} ... {}".format(n, total, name, note or "done"))

    result = shrink_folder(folder, opts["fit"], opts["quality"], progress=say)
    failed = sum(1 for r in result["rows"] if str(r[3]).startswith("failed"))

    report = Path(result["out"]) / "smaller.csv"
    try:
        Path(result["out"]).mkdir(parents=True, exist_ok=True)
        with open(report, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["photo", "bytes_before", "bytes_after", "result"])
            writer.writerows(result["rows"])
    except OSError as err:
        print("\ncould not write smaller.csv: {}".format(err))

    before, after = result["before"], result["after"]
    print("\n  {} photos   {} became {}".format(result["files"],
                                                mb(before), mb(after)))
    if before:
        print("  {:.0f}% smaller".format(100.0 * (before - after) / before))
    if failed:
        print("  {} could not be read and were left out".format(failed))
    print("\nOutput: {}".format(result["out"]))
    print("Your original photos have not been touched.")
    return (1 if failed else 0), opts["pause"]


def main(argv):
    try:
        paths, opts = parse_args(argv)
    except HelpWanted:
        print(USAGE)
        return 0, True
    except ArgError as err:
        print("parchi: {}\n".format(err))
        print(USAGE)
        return 2, True

    if not paths:
        print(USAGE)
        return 0, opts["pause"]

    files = list(collect(paths))
    if not files:
        print("No photos found.")
        return 0, opts["pause"]

    if opts["smaller"]:
        return shrink_main(paths, files, opts)

    outroot = opts["out"] or Path(paths[0]).parent / OUTDIR_NAME
    if Path(paths[0]).is_dir() and not opts["out"]:
        outroot = Path(paths[0]) / OUTDIR_NAME
    outroot.mkdir(parents=True, exist_ok=True)

    print("parchi {}   {} photos   confidence {:.0f}%, review {:.0f}%\n".format(
        __version__, len(files), opts["confidence"] * 100, opts["review"] * 100))

    counts = dict.fromkeys(BUCKETS, 0)
    failed = 0
    rows = []
    suspect = []
    bytes_in = bytes_out = 0

    # Photos a person has already settled.  Cropping them again would replace
    # somebody's afternoon with a guess, and nothing on screen would say so.
    if opts["redo"]:
        forget_all_decided(outroot)
        decided = {}
    else:
        decided = load_decided(outroot)
    if decided:
        print("  {} {} finished by hand before; leaving {} alone."
              "  Pass --redo to crop everything again.\n".format(
                  len(decided),
                  "photo was" if len(decided) == 1 else "photos were",
                  "it" if len(decided) == 1 else "them"))
    left_alone = 0

    for n, f in enumerate(files, 1):
        print("  [{}/{}] {} ... ".format(n, len(files), f.name), end="", flush=True)
        if f.name in decided:
            bucket = decided[f.name]
            if bucket in counts:
                counts[bucket] += 1
            left_alone += 1
            print("already finished by hand, left alone")
            rows.append([f.name, bucket, "", "finished by hand", f.name, "", ""])
            continue
        try:
            bucket, score, out, note, quad = process(f, outroot, opts)
        except Exception as err:
            print("failed: {}: {}".format(type(err).__name__, err))
            rows.append([f.name, "failed", "", "",
                         "{}: {}".format(type(err).__name__, err), "", ""])
            failed += 1
            continue
        counts[bucket] += 1
        if opts["audit"] and bucket == "1_cropped" and not note:
            try:
                suspect.append((f.name, audit_crop(load(f), quad)))
            except Exception:
                pass
        print("{}  ({:.0f}%){}".format(bucket.split("_", 1)[1], score * 100,
                                       "  " + note if note else ""))
        try:
            bytes_in += f.stat().st_size
            bytes_out += out.stat().st_size
        except OSError:
            pass
        rows.append([f.name, bucket, "{:.0f}".format(score * 100), note, out.name,
                     f.stat().st_size if f.exists() else "",
                     out.stat().st_size if out.exists() else ""])

    report = outroot / "report.csv"
    try:
        with open(report, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["photo", "result", "confidence_percent", "note",
                             "output", "bytes_before", "bytes_after"])
            writer.writerows(rows)
    except OSError as err:
        print("\ncould not write report.csv: {}".format(err))

    if left_alone:
        print("\n  {} {} already finished by hand and {} left alone.".format(
            left_alone,
            "photo was" if left_alone == 1 else "photos were",
            "was" if left_alone == 1 else "were"))

    total = max(1, len(files) - failed)
    print("\n  cropped        {:>4}   {:.0f}%".format(counts["1_cropped"],
                                                      100.0 * counts["1_cropped"] / total))
    print("  needs review   {:>4}   {:.0f}%".format(counts["2_review"],
                                                    100.0 * counts["2_review"] / total))
    print("  not detected   {:>4}   {:.0f}%".format(counts["3_not_detected"],
                                                    100.0 * counts["3_not_detected"] / total))
    if failed:
        print("  failed         {:>4}".format(failed))
    bad = []
    if opts["audit"]:
        bad = [(name, leak) for name, leak in suspect if leak > AUDIT_LEAK_LIMIT]
        print("\n  audit: {} crops checked, {} look like they cut into the bill".format(
            len(suspect), len(bad)))
        for name, leak in sorted(bad, key=lambda x: -x[1]):
            print("    {:<32} {:.0f}% of the paper left outside".format(name, leak * 100))
        if bad:
            print("    Look at these in {}/_debug before trusting the run.".format(outroot))

    if opts["pdf"]:
        pages = sorted((outroot / "1_cropped").glob("*"))
        pdf_path, message = build_pdf(pages, outroot / "all_pages.pdf",
                                      opts["page_size"])
        if pdf_path:
            print("\n  PDF: {}  ({})".format(pdf_path, message))
        else:
            print("\n  PDF not written: {}".format(message))

    if bytes_in:
        print("\n  size: {} in, {} out".format(mb(bytes_in), mb(bytes_out)))
        if bytes_out < bytes_in:
            print("        {:.0f}% smaller at {}px, quality {}".format(
                100.0 * (bytes_in - bytes_out) / bytes_in,
                opts["fit"], opts["quality"]))
        elif not opts["fit"]:
            print("        saved at full size")

    print("\nOutput: {}".format(outroot))
    print("Check report.csv, then look through 2_review and 3_not_detected.")
    return (1 if (failed or bad) else 0), opts["pause"]


def pause():
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\nPress Enter to close...")
    except (EOFError, KeyboardInterrupt, ValueError):
        pass


if __name__ == "__main__":
    wants_pause = True
    try:
        code, wants_pause = main(sys.argv[1:])
    except SystemExit as e:
        code = e.code or 0
    except KeyboardInterrupt:
        print("\ncancelled")
        code = 130
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    if wants_pause:
        pause()
    sys.exit(code)
