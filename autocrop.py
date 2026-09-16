"""
autocrop : trim uniform borders off images.

Drag image files or folders onto autocrop.exe, or run it from a command prompt:

    autocrop.exe <paths...> [--tol 12] [--pad 0] [--out DIR]

Cropped copies are written to a "_cropped" folder next to each source image.
Originals are never touched.
"""

import sys
from collections import Counter
from pathlib import Path

from PIL import Image, ImageChops

__version__ = "1.0"

EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
OUTDIR_NAME = "_cropped"

USAGE = """autocrop {v} : trim uniform borders off images

  Drag image files or folders onto autocrop.exe, or:
    autocrop.exe <files or folders...> [options]

  Options
    -t, --tol N     how different a pixel must be from the border colour
                    before it counts as content.  0-255, default 12.
                    Raise it for noisy scans, lower it for clean graphics.
    -p, --pad N     leave N pixels of border around the content.  Default 0.
    -o, --out DIR   write every crop into DIR instead of a "_cropped"
                    folder beside each source image.
        --no-pause  do not wait for a keypress at the end (for scripts).
    -h, --help      this text.

  Formats: {exts}
  Animated images are skipped.  Originals are never modified.
""".format(v=__version__, exts=", ".join(sorted(e[1:] for e in EXTS)))


class ArgError(Exception):
    pass


class HelpWanted(Exception):
    pass


# --------------------------------------------------------------------------
# finding the content
# --------------------------------------------------------------------------

def border_colour(rgb):
    """Most common colour around the outer edge of the image.

    Sampling the whole edge rather than the four corners survives a stray
    corner pixel, a logo in one corner, and JPEG noise.
    """
    w, h = rgb.size
    px = rgb.load()
    step = max(1, min(w, h) // 256)
    counts = Counter()
    for x in range(0, w, step):
        counts[px[x, 0]] += 1
        counts[px[x, h - 1]] += 1
    for y in range(0, h, step):
        counts[px[0, y]] += 1
        counts[px[w - 1, y]] += 1
    return counts.most_common(1)[0][0]


def content_box(im, tol):
    """Bounding box of everything that is not border, or None."""
    if im.mode == "P" and "transparency" in im.info:
        im = im.convert("RGBA")

    # A transparent border wins whenever the image actually has one.
    if im.mode in ("RGBA", "LA", "PA"):
        alpha = im.getchannel("A")
        if alpha.getextrema()[0] < 255:
            return alpha.point(lambda p: 255 if p > tol else 0).getbbox()

    rgb = im.convert("RGB")
    bg = border_colour(rgb)
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg))

    # Per-channel maximum, NOT convert("L").  Converting to luma weights blue
    # at 11% and green at 59%, so the same --tol would be five times stricter
    # on a blue subject than on a green one and would quietly eat it.
    r, g, b = diff.split()
    mask = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return mask.point(lambda p: 255 if p > tol else 0).getbbox()


# --------------------------------------------------------------------------
# saving
# --------------------------------------------------------------------------

def save_options(info, suffix):
    """Keep metadata and pick sane per-format encoder settings."""
    opts = {}
    if suffix in (".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".png"):
        if info.get("exif"):
            opts["exif"] = info["exif"]
        if info.get("icc_profile"):
            opts["icc_profile"] = info["icc_profile"]
    if suffix in (".jpg", ".jpeg"):
        opts.update(quality=95, subsampling=0, optimize=True)
    elif suffix == ".webp":
        opts.update(quality=95, method=4)
    elif suffix == ".png":
        opts["optimize"] = True
    return opts


def flatten_for_jpeg(im):
    """JPEG has no alpha.  Composite onto white, not onto black."""
    if im.mode == "P" and "transparency" in im.info:
        im = im.convert("RGBA")
    if im.mode in ("RGBA", "LA", "PA"):
        im = im.convert("RGBA")
        white = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(white, im)
    return im.convert("RGB")


def crop_one(path, tol, pad, outdir):
    """Crop one image.  Returns the output Path, or a reason string."""
    with Image.open(path) as im:
        if getattr(im, "n_frames", 1) > 1:
            return "animated"

        box = content_box(im, tol)
        if not box:
            return "blank"

        if pad:
            left, top, right, bottom = box
            box = (max(0, left - pad), max(0, top - pad),
                   min(im.width, right + pad), min(im.height, bottom + pad))

        if box == (0, 0, im.width, im.height):
            return "full"

        cropped = im.crop(box)
        info = dict(im.info)

    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg") and cropped.mode not in ("RGB", "L", "CMYK"):
        cropped = flatten_for_jpeg(cropped)

    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / path.name
    cropped.save(out, **save_options(info, suffix))
    return out


# --------------------------------------------------------------------------
# gathering inputs
# --------------------------------------------------------------------------

def collect(paths):
    """Yield every supported image under the given files and folders, once."""
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
            if f.suffix.lower() not in EXTS:
                continue
            if OUTDIR_NAME in f.parts:      # never re-crop our own output
                continue
            try:
                key = f.resolve()
            except OSError:
                key = f.absolute()
            if key in seen:                 # folder + a file inside it
                continue
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
    paths, tol, pad, out, pause = [], 12, 0, None, True
    i = 0
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("-") and arg not in ("/?",):
            paths.append(arg)
            i += 1
            continue

        name, sep, inline = arg.partition("=")

        def value():
            if sep:
                return inline
            if i + 1 >= len(argv):
                raise ArgError("{} needs a value, for example {} 12".format(name, name))
            return argv[i + 1]

        if name in ("-h", "--help", "/?"):
            raise HelpWanted()
        elif name in ("-t", "--tol"):
            tol = as_int(name, value(), 0, 255)
            i += 0 if sep else 1
        elif name in ("-p", "--pad"):
            pad = as_int(name, value(), 0, 10000)
            i += 0 if sep else 1
        elif name in ("-o", "--out"):
            out = Path(value())
            i += 0 if sep else 1
        elif name == "--no-pause":
            pause = False
        elif name in ("-v", "--version"):
            print("autocrop {}".format(__version__))
            raise SystemExit(0)
        else:
            raise ArgError("unknown option: {}".format(arg))
        i += 1
    return paths, tol, pad, out, pause


def main(argv):
    try:
        paths, tol, pad, out, wants_pause = parse_args(argv)
    except HelpWanted:
        print(USAGE)
        return 0, True
    except ArgError as err:
        print("autocrop: {}\n".format(err))
        print(USAGE)
        return 2, True

    if not paths:
        print(USAGE)
        return 0, wants_pause

    files = list(collect(paths))
    if not files:
        print("No supported images found.")
        return 0, wants_pause

    print("autocrop {}  (tol={}, pad={})\n".format(__version__, tol, pad))

    reasons = {"animated": "skipped (animated)",
               "blank": "skipped (nothing but border)",
               "full": "skipped (no border to trim)"}
    done = skipped = failed = 0

    for f in files:
        print("  {} ... ".format(f.name), end="", flush=True)
        try:
            result = crop_one(f, tol, pad, out or f.parent / OUTDIR_NAME)
        except Exception as err:
            print("failed: {}: {}".format(type(err).__name__, err))
            failed += 1
            continue
        if isinstance(result, Path):
            print("cropped")
            done += 1
        else:
            print(reasons.get(result, "skipped"))
            skipped += 1

    print("\n{} cropped, {} skipped, {} failed.".format(done, skipped, failed))
    if done:
        where = str(out) if out else '"{}" folder beside each image'.format(OUTDIR_NAME)
        print("Output: {}".format(where))
    return (1 if failed else 0), wants_pause


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
