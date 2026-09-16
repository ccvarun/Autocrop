"""
selftest.py : build sample images, run autocrop over them, check the results.

    python selftest.py

Prints one line per check and exits non-zero if anything failed.
Nothing outside the temporary folder is touched.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "autocrop.py"

passed = failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  ok    {}".format(name))
    else:
        failed += 1
        print("  FAIL  {}  {}".format(name, detail))


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), "--no-pause", *map(str, args)],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


def make(work):
    """Sample images covering the cases that used to break."""
    # plain: black square on a white field
    im = Image.new("RGB", (200, 200), (255, 255, 255))
    im.paste((0, 0, 0), (50, 50, 150, 150))
    im.save(work / "plain.png")

    # blue: a blue subject on black, the case luma weighting used to destroy
    im = Image.new("RGB", (200, 200), (0, 0, 0))
    im.paste((0, 0, 40), (50, 50, 150, 150))
    im.save(work / "blue.png")

    # alpha: transparent border
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    im.paste((10, 20, 200, 255), (60, 60, 140, 140))
    im.save(work / "alpha.png")

    # exif: JPEG carrying an orientation tag that must survive
    im = Image.new("RGB", (200, 200), (255, 255, 255))
    im.paste((0, 0, 0), (40, 40, 160, 160))
    exif = Image.Exif()
    exif[274] = 6
    im.save(work / "exif.jpg", exif=exif)

    # palette image with a transparent border
    p = Image.new("P", (100, 100), 0)
    p.putpalette([0, 0, 0] + [255, 0, 0] + [0] * (256 * 3 - 6))
    p.paste(1, (30, 30, 70, 70))
    p.info["transparency"] = 0
    p.save(work / "palette.png")

    # animated webp, must be skipped rather than silently flattened
    frames = [Image.new("RGB", (100, 100), c)
              for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    for f in frames:
        f.paste((0, 0, 0), (30, 30, 70, 70))
    frames[0].save(work / "anim.webp", save_all=True,
                   append_images=frames[1:], duration=200, loop=0)

    # uniform image, nothing to find
    Image.new("RGB", (80, 80), (200, 200, 200)).save(work / "uniform.png")

    # a stray corner pixel must not fool border detection
    im = Image.new("RGB", (200, 200), (255, 255, 255))
    im.paste((0, 0, 0), (50, 50, 150, 150))
    im.putpixel((0, 0), (7, 3, 250))
    im.save(work / "speck.png")


def main():
    work = Path(tempfile.mkdtemp(prefix="autocrop-selftest-"))
    try:
        make(work)
        result = run(work)
        out = work / "_cropped"
        print("\n[crop results]")

        check("exit code 0", result.returncode == 0, result.stderr.strip()[:200])
        check("plain.png trimmed to content",
              (out / "plain.png").exists() and Image.open(out / "plain.png").size == (100, 100))
        check("blue subject survives luma trap",
              (out / "blue.png").exists() and Image.open(out / "blue.png").size == (100, 100),
              "a blue subject was eaten by the threshold")
        check("transparent border trimmed",
              (out / "alpha.png").exists() and Image.open(out / "alpha.png").size == (80, 80))
        check("palette transparency handled",
              (out / "palette.png").exists() and Image.open(out / "palette.png").size == (40, 40))
        check("exif orientation preserved",
              (out / "exif.jpg").exists()
              and Image.open(out / "exif.jpg").getexif().get(274) == 6,
              "orientation tag was dropped")
        check("animated image left alone", not (out / "anim.webp").exists())
        check("animated image reported", "skipped (animated)" in result.stdout)
        check("uniform image left alone", not (out / "uniform.png").exists())
        check("stray corner pixel ignored",
              (out / "speck.png").exists() and Image.open(out / "speck.png").size == (150, 150),
              "border colour was taken from one odd pixel")

        print("\n[input handling]")
        again = run(work, work / "plain.png")
        check("no duplicate work", again.stdout.count("plain.png ...") == 1)
        check("own output not re-cropped", not (out / "_cropped").exists())

        bad = run(work, "--tol")
        check("missing value is a clean error",
              bad.returncode == 2 and "needs a value" in bad.stdout and not bad.stderr.strip())
        bad = run(work, "--tol", "abc")
        check("bad value is a clean error",
              bad.returncode == 2 and "whole number" in bad.stdout and not bad.stderr.strip())
        bad = run(work, "--tol", "900")
        check("out of range value rejected",
              bad.returncode == 2 and "between 0 and 255" in bad.stdout)
        bad = run(work, "--wat")
        check("unknown option rejected",
              bad.returncode == 2 and "unknown option" in bad.stdout)
        helped = run("--help")
        check("help works", helped.returncode == 0 and "--tol" in helped.stdout)
        check("inline form --tol=30 accepted", run(work, "--tol=30").returncode == 0)
        check("missing path reported", "not found" in run(work / "nope.png").stdout)

        print("\n[options]")
        elsewhere = work / "elsewhere"
        r = run(work / "plain.png", "--out", elsewhere)
        check("--out writes where asked", r.returncode == 0 and (elsewhere / "plain.png").exists())
        padded = work / "padded"
        run(work / "plain.png", "--out", padded, "--pad", "8")
        check("--pad adds a margin",
              (padded / "plain.png").exists() and Image.open(padded / "plain.png").size == (116, 116))

        print("\n{} passed, {} failed.".format(passed, failed))
        return 1 if failed else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
