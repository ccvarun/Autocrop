"""
cropui : local review page for parchi.

Start it with START_Crop.bat, stop it with STOP_Crop.bat.  It serves a page on
127.0.0.1 that lets a person pick a folder of parchi photos, run detection, and
then fix by hand the ones detection was not sure about.

The review step is the point.  Detection on uncontrolled phone photos will
never be perfect, so what matters is that correcting a photo takes seconds:
drag four corners, press Crop.  Everything the tool was unsure about is a short
queue rather than a pile to redo elsewhere.

Nothing is ever destroyed.  Originals stay where they are; every crop is
written into the _parchi output folders beside them.
"""

import csv
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

import parchi

PORT = int(os.environ.get("CROP_UI_PORT", "8112"))
HOST = "127.0.0.1"

app = FastAPI(title="Crop")

# Single local user, so a module-level job is enough.
JOB = {
    "root": None,          # folder of photos being worked on
    "outroot": None,       # its _parchi folder
    "running": False,
    "done": 0,
    "total": 0,
    "log": [],
    "results": {},         # name -> {bucket, score, quad, width, height}
    "leftAlone": 0,        # photos a person had already finished, not touched
    "opts": {"confidence": 0.62, "review": 0.35, "margin": 1, "enhance": False,
             "upright": False, "brightness": 0, "contrast": 1.0,
             "fit": parchi.FIT_LONGEST, "quality": parchi.FIT_QUALITY},
    "shrink": None,        # the "make photos smaller" job, when one is running
}
LOCK = threading.Lock()


def here(*parts):
    """Resource path that also works inside a PyInstaller one-file exe."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def inside_root(path):
    """Refuse anything outside the folder the user chose.

    The page is only bound to localhost, but a path parameter that can read any
    file on the machine is not something to leave lying around regardless.
    """
    root = JOB["root"]
    if root is None:
        raise HTTPException(400, "no folder chosen yet")
    resolved = Path(path).resolve()
    if root not in resolved.parents and resolved != root:
        raise HTTPException(403, "outside the chosen folder")
    return resolved


# --------------------------------------------------------------------------
# picking a folder
# --------------------------------------------------------------------------

def drives():
    """Drives, with the handful of folders photos actually arrive in first."""
    places = []
    home = Path.home()
    for label in ("Desktop", "Downloads", "Documents", "Pictures", "OneDrive"):
        candidate = home / label
        if candidate.is_dir():
            places.append({"name": label, "path": str(candidate)})

    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = "{}:\\".format(letter)
            if os.path.isdir(root):
                places.append({"name": root, "path": root})
    else:
        places.append({"name": "/", "path": "/"})
    return places


@app.get("/api/browse")
def browse(path: str = ""):
    """Server-side folder navigator, so nobody has to type a path."""
    if not path:
        return {"path": "", "up": None, "dirs": drives(), "images": 0}
    p = Path(path)
    if not p.is_dir():
        raise HTTPException(404, "not a folder")
    dirs = []
    images = 0
    try:
        for entry in sorted(p.iterdir(), key=lambda e: e.name.lower()):
            if entry.name.startswith(".") or entry.name == parchi.OUTDIR_NAME:
                continue
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
                elif entry.suffix.lower() in parchi.EXTS:
                    images += 1
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, "cannot open that folder")
    parent = str(p.parent) if p.parent != p else ""
    return {"path": str(p), "up": parent, "dirs": dirs, "images": images}


class ResolveRequest(BaseModel):
    text: str


def folder_report(p):
    images = 0
    try:
        for entry in p.iterdir():
            if entry.is_file() and entry.suffix.lower() in parchi.EXTS:
                images += 1
    except OSError:
        pass
    return {"path": str(p), "images": images}


@app.post("/api/resolve")
def resolve(req: ResolveRequest):
    """Turn whatever a drop gave us into a real folder on this machine.

    Browsers deliberately hide the path of a dropped folder.  Windows Explorer,
    however, usually puts the path into the drag as text as well, so when that
    text is there a drop can be honoured exactly.  When it is not, the page
    falls back to the native picker rather than guessing.
    """
    text = (req.text or "").strip().strip('"').splitlines()
    text = [line.strip() for line in text if line.strip() and not line.startswith("#")]
    if not text:
        raise HTTPException(400, "nothing usable in that drop")

    candidate = text[0]
    if candidate.lower().startswith("file:"):
        parsed = urllib.parse.urlparse(candidate)
        candidate = urllib.request.url2pathname(parsed.path)
        if parsed.netloc:                       # file://server/share
            candidate = "\\\\" + parsed.netloc + candidate

    p = Path(candidate)
    if p.is_file():                             # a photo was dropped, use its folder
        p = p.parent
    if not p.is_dir():
        raise HTTPException(404, "that is not a folder on this machine")
    return folder_report(p)


# The modern Explorer-style chooser is IFileOpenDialog with FOS_PICKFOLDERS.
# Windows PowerShell's own FolderBrowserDialog is the tree-view box from
# Windows 2000: no address bar, so no pasting a path and a long scroll to reach
# a mapped drive.  This asks for the modern one and quietly falls back to the
# old one if anything about the interop fails, so the picker always works.
PICKER_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms

function Pick-Modern {
  Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class ModernPicker {
    [ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")]
    internal class FileOpenDialogRCW { }

    [ComImport, Guid("42f85136-db7e-439c-85f1-e4075d135fc8"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IFileDialog {
        [PreserveSig] uint Show([In, Optional] IntPtr hwndOwner);
        void SetFileTypes(uint cFileTypes, IntPtr rgFilterSpec);
        void SetFileTypeIndex([In] uint iFileType);
        void GetFileTypeIndex(out uint piFileType);
        void Advise(IntPtr pfde, out uint pdwCookie);
        void Unadvise([In] uint dwCookie);
        void SetOptions([In] uint fos);
        void GetOptions(out uint pfos);
        void SetDefaultFolder(IShellItem psi);
        void SetFolder(IShellItem psi);
        void GetFolder(out IShellItem ppsi);
        void GetCurrentSelection(out IShellItem ppsi);
        void SetFileName([In, MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string pszName);
        void SetTitle([In, MarshalAs(UnmanagedType.LPWStr)] string pszTitle);
        void SetOkButtonLabel([In, MarshalAs(UnmanagedType.LPWStr)] string pszText);
        void SetFileNameLabel([In, MarshalAs(UnmanagedType.LPWStr)] string pszLabel);
        void GetResult(out IShellItem ppsi);
        void AddPlace(IShellItem psi, uint fdap);
        void SetDefaultExtension([In, MarshalAs(UnmanagedType.LPWStr)] string pszDefaultExtension);
        void Close([MarshalAs(UnmanagedType.Error)] uint hr);
        void SetClientGuid([In] ref Guid guid);
        void ClearClientData();
        void SetFilter([MarshalAs(UnmanagedType.Interface)] IntPtr pFilter);
    }

    [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IShellItem {
        void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
        void GetParent(out IShellItem ppsi);
        void GetDisplayName([In] uint sigdnName,
                            [MarshalAs(UnmanagedType.LPWStr)] out string ppszName);
        void GetAttributes([In] uint sfgaoMask, out uint psfgaoAttribs);
        void Compare(IShellItem psi, uint hint, out int piOrder);
    }

    const uint FOS_PICKFOLDERS     = 0x00000020;
    const uint FOS_FORCEFILESYSTEM = 0x00000040;
    const uint FOS_PATHMUSTEXIST   = 0x00000800;
    const uint SIGDN_FILESYSPATH   = 0x80058000;

    public static string Pick(string title) {
        IFileDialog dialog = (IFileDialog)new FileOpenDialogRCW();
        dialog.SetOptions(FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST);
        dialog.SetTitle(title);
        dialog.SetOkButtonLabel("Use this folder");
        uint hr = dialog.Show(IntPtr.Zero);
        if (hr != 0) { return ""; }            // cancelled
        IShellItem item;
        dialog.GetResult(out item);
        string path;
        item.GetDisplayName(SIGDN_FILESYSPATH, out path);
        return path;
    }
}
'@
  return [ModernPicker]::Pick('Choose the folder of parchi photos')
}

function Pick-Classic {
  $d = New-Object System.Windows.Forms.FolderBrowserDialog
  $d.Description = 'Choose the folder of parchi photos'
  $d.ShowNewFolderButton = $false
  $top = New-Object System.Windows.Forms.Form
  $top.TopMost = $true
  if ($d.ShowDialog($top) -eq 'OK') { return $d.SelectedPath }
  return ''
}

try {
  $picked = Pick-Modern
  [Console]::Out.Write("MODERN|" + $picked)
} catch {
  try {
    $picked = Pick-Classic
    [Console]::Out.Write("CLASSIC|" + $picked)
  } catch {
    [Console]::Out.Write("FAILED|")
  }
}
"""


@app.post("/api/pick")
def pick():
    """Open the machine's own folder chooser.

    The page cannot open a native dialog, but the server is running on the same
    machine, so it can.  This is what the Browse button uses.
    """
    if os.name != "nt":
        raise HTTPException(501, "native folder picker is Windows only")

    script = Path(tempfile.gettempdir()) / "crop_pick_folder.ps1"
    try:
        script.write_text(PICKER_SCRIPT, encoding="utf-8")
        done = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True, timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as err:
        raise HTTPException(500, "could not open the folder chooser: {}".format(err))
    finally:
        try:
            script.unlink()
        except OSError:
            pass

    raw = (done.stdout or "").strip()
    style, _, chosen = raw.partition("|")
    chosen = chosen.strip()
    if style == "FAILED":
        raise HTTPException(500, "the folder chooser would not open: {}".format(
            (done.stderr or "").strip()[:200]))
    if not chosen:
        return {"cancelled": True, "style": style.lower()}
    p = Path(chosen)
    if not p.is_dir():
        raise HTTPException(404, "that folder is not there any more")
    report = folder_report(p)
    report["style"] = style.lower()          # "modern" or "classic", for support
    return report


# --------------------------------------------------------------------------
# running detection
# --------------------------------------------------------------------------

class RunRequest(BaseModel):
    folder: str
    confidence: int = 62
    review: int = 35
    upright: bool = False
    margin: int = 1
    enhance: bool = False
    brightness: int = 0
    contrast: float = 1.0
    redo: bool = False
    fit: int = parchi.FIT_LONGEST
    quality: int = parchi.FIT_QUALITY


def run_job(folder, opts):
    root = Path(folder)
    outroot = root / parchi.OUTDIR_NAME
    files = list(parchi.collect([str(root)]))

    # What a person already settled by hand, on this folder, in some earlier
    # session.  Running again used to crop these afresh and write the machine's
    # answer over theirs: the file changed on disk, the page said "cropped",
    # and the only clue was the size.  Their work wins.
    if opts.get("redo"):
        parchi.forget_all_decided(outroot)
        decided = {}
    else:
        decided = parchi.load_decided(outroot)

    with LOCK:
        JOB.update(root=root.resolve(), outroot=outroot, running=True,
                   done=0, total=len(files), log=[], results={}, opts=opts,
                   leftAlone=0)

    for f in files:
        if f.name in decided:
            entry = settled_entry(f, decided[f.name])
            line = "{}  already finished by hand, left alone".format(f.name)
            with LOCK:
                JOB["results"][f.name] = entry
                JOB["log"].append(line)
                JOB["done"] += 1
                JOB["leftAlone"] = JOB.get("leftAlone", 0) + 1
            continue
        try:
            bgr = parchi.load(f)
            quad, score = parchi.detect(bgr)
            bucket, note = parchi.decide(bgr, quad, score,
                                         opts["confidence"], opts["review"])
            outdir = outroot / bucket
            outdir.mkdir(parents=True, exist_ok=True)
            if bucket == "1_cropped" and not note:
                result = parchi.warp(bgr, quad, opts["margin"])
                if opts.get("upright"):
                    result = parchi.stand_upright(result)
                result = parchi.finish(result, opts)
                parchi.put_image(result, outdir / f.name, opts)
            else:
                parchi.put_original(f, outdir / f.name, opts, bgr)
            h, w = bgr.shape[:2]
            # For a photo where the paper runs past the picture there are no
            # four corners to place, but there is still background on the
            # sides that do show.  Offer the operator that trim as the outline
            # to start from; it is never applied without them.
            trim = parchi.trim_to_frame(quad, bgr.shape)
            runs_off = parchi.quad_touches_frame(quad, bgr.shape)
            # how much of the frame the trim would actually remove.  On a
            # photo whose paper runs off all four edges this is zero, and a
            # trim that removes nothing must not be announced as one.
            trim_takes = 0.0
            if trim is not None:
                # float(): the quad is float32 and numpy scalars do not
                # survive JSON encoding, which takes /api/results down
                kept = float((trim[2][0] - trim[0][0])
                             * (trim[2][1] - trim[0][1])) / float(h * w)
                trim_takes = round(max(0.0, 1.0 - kept), 3)
            entry = {"bucket": bucket, "note": note, "score": round(score * 100),
                     "quad": quad.tolist() if quad is not None else None,
                     "trim": trim.tolist() if trim is not None else None,
                     "runsOff": runs_off, "trimTakes": trim_takes,
                     "width": w, "height": h, "source": str(f), "edited": False,
                     # where this photo started, so an accidental crop can be undone
                     "origin": {"bucket": bucket,
                                "quad": quad.tolist() if quad is not None else None}}
            line = "{}  {}  {}%{}".format(f.name, bucket.split("_", 1)[1],
                                          round(score * 100),
                                          "  " + note if note else "")
        except Exception as err:
            # Only detection may have failed; the photo itself often loads
            # perfectly.  Recording zero for its size makes the editor fold
            # all four corners onto one point, and the page then blames the
            # person for corners it placed there itself.  Ask the photo.
            try:
                fh, fw = parchi.load(f).shape[:2]
            except Exception:
                fh, fw = 0, 0
            entry = {"bucket": "failed", "note": "", "score": 0, "quad": None,
                     "width": fw, "height": fh, "source": str(f), "edited": False,
                     "error": "{}: {}".format(type(err).__name__, err)}
            line = "{}  failed: {}".format(f.name, err)
        with LOCK:
            JOB["results"][f.name] = entry
            JOB["log"].append(line)
            JOB["done"] += 1

    with LOCK:
        JOB["running"] = False


def settled_entry(path, bucket):
    """A photo a person finished earlier: shown, listed, and not touched.

    Detection is skipped deliberately.  It is the expensive part, and its
    answer would only be a worse one than the person already gave.  The photo
    is still measured so the editor can open on it if they want another go.
    """
    try:
        h, w = parchi.load(path).shape[:2]
    except Exception:
        h, w = 0, 0
    return {"bucket": bucket, "note": "finished by hand", "score": 100,
            "quad": None, "trim": None, "runsOff": 0, "trimTakes": 0.0,
            "width": w, "height": h, "source": str(path), "edited": True,
            # No origin: this session did not crop it, so this session has
            # nothing to put back.  Undo says so rather than inventing a copy
            # of the original and calling it the earlier work.
            "settled": True}


@app.post("/api/run")
def start_run(req: RunRequest):
    if JOB["running"]:
        raise HTTPException(409, "already running")
    if not Path(req.folder).is_dir():
        raise HTTPException(400, "not a folder")
    if req.review > req.confidence:
        raise HTTPException(400, "review threshold cannot be above confidence")
    opts = {"confidence": req.confidence / 100.0, "review": req.review / 100.0,
            "upright": bool(req.upright),
            "margin": req.margin, "enhance": req.enhance,
            "brightness": int(req.brightness), "contrast": float(req.contrast),
            "redo": bool(req.redo),
            "fit": max(0, int(req.fit)), "quality": int(req.quality)}
    threading.Thread(target=run_job, args=(req.folder, opts), daemon=True).start()
    time.sleep(0.2)
    return {"ok": True}


class ShrinkRequest(BaseModel):
    folder: str
    fit: int = parchi.FIT_LONGEST
    quality: int = parchi.FIT_QUALITY


def shrink_job(folder, fit, quality):
    """The whole of "make photos smaller": no detection, no queue, no review."""
    def step(n, total, name, note):
        with LOCK:
            job = JOB["shrink"]
            if job is None:
                return
            job["done"] = n
            job["total"] = total
            job["log"].append("{}  {}".format(name, note or "done"))
            job["log"] = job["log"][-12:]
    try:
        result = parchi.shrink_folder(folder, fit, quality, progress=step)
        try:
            out = Path(result["out"])
            out.mkdir(parents=True, exist_ok=True)
            with (out / "smaller.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["photo", "bytes_before", "bytes_after", "result"])
                writer.writerows(result["rows"])
        except OSError:
            pass                     # a missing report must not fail the job
        failed = [r[0] for r in result["rows"] if str(r[3]).startswith("failed")]
        with LOCK:
            JOB["shrink"].update(running=False, done=result["files"],
                                 total=result["files"],
                                 before=result["before"], after=result["after"],
                                 out=result["out"], failed=failed)
    except Exception as err:
        with LOCK:
            JOB["shrink"].update(running=False,
                                 error="{}: {}".format(type(err).__name__, err))


@app.post("/api/shrink")
def start_shrink(req: ShrinkRequest):
    with LOCK:
        busy = JOB["running"] or (JOB["shrink"] or {}).get("running")
    if busy:
        raise HTTPException(409, "already running")
    folder = Path(req.folder)
    if not folder.is_dir():
        raise HTTPException(400, "not a folder")
    fit = max(0, int(req.fit))
    quality = int(req.quality)
    if not 40 <= quality <= 100:
        raise HTTPException(400, "quality must be between 40 and 100")
    files = list(parchi.collect([str(folder)]))
    if not files:
        raise HTTPException(400, "no photos in that folder")
    with LOCK:
        JOB["shrink"] = {"running": True, "done": 0, "total": len(files),
                         "log": [], "folder": str(folder),
                         "out": str(parchi.smaller_root(folder)),
                         "before": 0, "after": 0, "failed": [], "error": ""}
    threading.Thread(target=shrink_job, args=(str(folder), fit, quality),
                     daemon=True).start()
    time.sleep(0.2)
    return {"ok": True, "total": len(files),
            "out": str(parchi.smaller_root(folder))}


@app.get("/api/shrink/progress")
def shrink_progress():
    with LOCK:
        job = JOB["shrink"]
        return dict(job) if job else {"running": False, "done": 0, "total": 0}


@app.get("/api/progress")
def progress():
    with LOCK:
        counts = {}
        for entry in JOB["results"].values():
            counts[entry["bucket"]] = counts.get(entry["bucket"], 0) + 1
        return {"running": JOB["running"], "done": JOB["done"], "total": JOB["total"],
                "log": JOB["log"][-12:], "counts": counts,
                "leftAlone": JOB.get("leftAlone", 0),
                "folder": str(JOB["root"]) if JOB["root"] else ""}


@app.get("/api/results")
def results():
    with LOCK:
        return {"folder": str(JOB["root"]) if JOB["root"] else "",
                "items": [dict(name=name, **entry)
                          for name, entry in sorted(JOB["results"].items())]}


# --------------------------------------------------------------------------
# images
# --------------------------------------------------------------------------

def encode(bgr, quality=82):
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise HTTPException(500, "could not encode image")
    return Response(content=buf.tobytes(), media_type="image/jpeg")


@app.get("/api/photo")
def photo(name: str, kind: str = "thumb", size: int = 1400, rotate: int = 0):
    with LOCK:
        entry = JOB["results"].get(name)
    if entry is None:
        raise HTTPException(404, "unknown photo")

    if kind == "output":
        path = JOB["outroot"] / entry["bucket"] / name
        if not path.exists():
            raise HTTPException(404, "no output for that photo")
        src = inside_root(path)
    else:
        src = inside_root(entry["source"])

    bgr = parchi.turn(parchi.load(src), rotate)
    limit = 320 if kind == "thumb" else size
    h, w = bgr.shape[:2]
    scale = limit / float(max(h, w))
    if scale < 1.0:
        bgr = cv2.resize(bgr, (int(w * scale), int(h * scale)),
                         interpolation=cv2.INTER_AREA)
    return encode(bgr, 78 if kind == "thumb" else 88)


# --------------------------------------------------------------------------
# fixing a photo by hand
# --------------------------------------------------------------------------

# Decoding a 12 megapixel photo takes long enough to feel laggy when the magnet
# fires on every handle release, and it is always the same photo being edited.
_LAST = {"key": None, "image": None}


def cached_image(name, path, rotate=0):
    """The photo as the operator is looking at it, quarter turns included.

    Rotation is applied to the PHOTO, not to the finished crop, so that the
    outline, the edge map, the loupe and the corners the page sends all live
    in one frame.  The alternative, turning only the result, leaves the two
    panes at different angles and makes every pointer coordinate a
    transform waiting to be got wrong.
    """
    turns = int(rotate) % 360
    key = (name, turns)
    if _LAST["key"] != key:
        _LAST["key"] = key
        _LAST["image"] = parchi.turn(parchi.load(path), turns)
    return _LAST["image"]


@app.get("/api/edges")
def edges(name: str, size: int = 700, rotate: int = 0):
    """A small picture of where this photo's edges are.

    Sent once when a photo opens, so the page can make the outline stick to
    real edges while a corner is being dragged.  Asking the server on every
    mouse move would be a round trip per pixel and would feel like treacle;
    with the edge map in hand the browser can snap instantly.
    """
    with LOCK:
        entry = JOB["results"].get(name)
    if entry is None:
        raise HTTPException(404, "unknown photo")

    bgr = cached_image(name, inside_root(entry["source"]), rotate)
    height, width = bgr.shape[:2]
    scale = float(size) / max(height, width)
    work = cv2.resize(bgr, (max(1, int(width * scale)), max(1, int(height * scale))),
                      interpolation=cv2.INTER_AREA) if scale < 1.0 else bgr

    gray = cv2.GaussianBlur(cv2.cvtColor(work, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = cv2.magnitude(gx, gy)

    # Include the soft, wide gradient a sheet casts as a shadow, not only the
    # sharp step at a lit edge: on a white slip on a white desk the shadow is
    # the only edge there is.
    soft = cv2.GaussianBlur(gray, (0, 0), 9)
    sx = cv2.Sobel(soft, cv2.CV_32F, 1, 0, ksize=5)
    sy = cv2.Sobel(soft, cv2.CV_32F, 0, 1, ksize=5)
    magnitude = cv2.max(magnitude / max(1e-6, float(np.percentile(magnitude, 99))),
                        cv2.magnitude(sx, sy) / max(1e-6, float(np.percentile(
                            cv2.magnitude(sx, sy), 99))))

    picture = np.clip(magnitude * 255.0, 0, 255).astype(np.uint8)
    ok, buf = cv2.imencode(".png", picture)
    if not ok:
        raise HTTPException(500, "could not build the edge map")
    return Response(content=buf.tobytes(), media_type="image/png",
                    headers={"X-Edge-Width": str(picture.shape[1]),
                             "X-Edge-Height": str(picture.shape[0])})


class SnapRequest(BaseModel):
    rotate: int = 0
    name: str
    quad: list


@app.post("/api/snap")
def snap(req: SnapRequest):
    """Pull the outline onto the paper's real edges."""
    with LOCK:
        entry = JOB["results"].get(req.name)
    if entry is None:
        raise HTTPException(404, "unknown photo")
    bgr = cached_image(req.name, inside_root(entry["source"]), req.rotate)
    points = usable_quad(np.array(req.quad, dtype="float32"), bgr.shape)

    snapped, shift = parchi.snap_to_edges(bgr, points)
    # Two sides that come out nearly parallel intersect a very long way away,
    # and the outline that comes back can be unusable.  Handing that to the
    # page poisons every preview after it, and the page then reports it as
    # corners the person placed badly.  Refuse to return a quad we would not
    # accept back, and leave the outline where it was.
    try:
        snapped = usable_quad(snapped, bgr.shape)
    except HTTPException:
        return {"quad": points.tolist(), "moved": 0.0,
                "note": "the edges did not give a usable outline"}
    return {"quad": snapped.tolist(), "moved": round(float(shift), 1)}


def usable_quad(points, shape):
    """Reject corners that do not describe a real crop.

    Without this, a collapsed outline quietly produces a ten pixel image and
    files it as a finished bill.
    """
    height, width = shape[:2]
    points = np.asarray(points, dtype="float32")
    if points.size != 8:
        raise HTTPException(400, "expected four corners, got {}"
                                 .format(points.size // 2))
    points = points.reshape(4, 2)

    # A corner that arrived as null or NaN used to fall through to the area
    # check, where every comparison against NaN is False, and come back out
    # the far side reported as "drag the corners apart".  That sent someone
    # looking for a mistake they had not made.  Say what actually happened.
    if not np.isfinite(points).all():
        raise HTTPException(400, "corner coordinates are not numbers")

    points[:, 0] = np.clip(points[:, 0], 0, width)
    points[:, 1] = np.clip(points[:, 1], 0, height)
    area = abs(cv2.contourArea(parchi.order_corners(points)))
    if area < 0.002 * width * height:
        raise HTTPException(400, "those corners enclose almost nothing")
    sides = parchi.side_lengths(parchi.order_corners(points))
    if min(sides) < 12:
        raise HTTPException(400, "two corners are almost on top of each other")
    return points


class PreviewRequest(BaseModel):
    name: str
    quad: list
    size: int = 900     # longest side of the picture sent back
    rotate: int = 0     # quarter turns applied to the photo, 0/90/180/270
    brightness: int = 0
    contrast: float = 1.0


# A preview is looked at and thrown away, so there is no reason to warp twelve
# megapixels and then discard nine tenths of them.  Warping a scaled-down copy
# gives the same picture for a fraction of the work, and this runs again every
# time a corner moves, so the fraction is the whole point.
_PREVIEW_SRC = {"key": None, "image": None, "scale": 1.0, "want": 0}


def look(req):
    """How this photo should be finished: the batch setting, with whatever
    the operator has changed for this one photo on top."""
    opts = dict(JOB["opts"])
    opts["brightness"] = int(getattr(req, "brightness", 0))
    opts["contrast"] = float(getattr(req, "contrast", 1.0))
    return opts


def preview_source(name, bgr, want, rotate=0):
    """A copy of the photo small enough to warp quickly, and how much it shrank.

    The crop is only part of the frame, so the source needs headroom above the
    size we want back; 2.5x leaves the result sharp for any parchi filling more
    than about a third of the photo, which is nearly all of them.
    """
    # Keyed on the TURN as well as the name.  Keyed on the name alone, a
    # request for the turned photo was served the unturned copy, and the
    # corners then landed somewhere else entirely.  The same mistake as
    # cached_image, made twice, so the key now carries everything the picture
    # depends on.
    key = (name, int(rotate) % 360)
    if _PREVIEW_SRC["key"] != key or want > _PREVIEW_SRC["want"]:
        scale = min(1.0, (want * 2.5) / float(max(bgr.shape[:2])))
        small = bgr if scale >= 1.0 else cv2.resize(
            bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        _PREVIEW_SRC.update(key=key, image=small, want=want,
                            scale=1.0 if scale >= 1.0 else scale)
    return _PREVIEW_SRC["image"], _PREVIEW_SRC["scale"]


@app.post("/api/preview")
def preview(req: PreviewRequest):
    """What the crop would look like, without writing anything.

    Shown beside the photo while the corners are being dragged.  Judging a crop
    from four handles on a busy photo is guesswork; judging it from the result
    is not.
    """
    with LOCK:
        entry = JOB["results"].get(req.name)
    if entry is None:
        raise HTTPException(404, "unknown photo")
    # usable_quad does the reshaping: doing it here first turns a request
    # with the wrong number of corners into a 500 instead of a clear refusal.
    points = np.array(req.quad, dtype="float32")
    bgr = cached_image(req.name, inside_root(entry["source"]), req.rotate)
    points = usable_quad(points, bgr.shape)

    want = max(240, min(1600, int(req.size)))
    small, scale = preview_source(req.name, bgr, want, req.rotate)
    try:
        result = parchi.warp(small, points * scale, JOB["opts"]["margin"])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "those corners do not make a crop")
    # The pane says "what you will get", so it has to include everything the
    # saved file will have.  It used to skip the contrast lift entirely.
    result = parchi.finish(result, look(req))
    limit = float(want) / max(result.shape[:2])
    if limit < 1.0:
        result = cv2.resize(result, None, fx=limit, fy=limit, interpolation=cv2.INTER_AREA)
    return encode(result, 80)


class CropRequest(BaseModel):
    name: str
    quad: list          # eight numbers, in original image pixels
    rotate: int = 0     # quarter turns applied to the photo, 0/90/180/270
    brightness: int = 0
    contrast: float = 1.0
    full_size: bool = False


class KeepRequest(BaseModel):
    name: str
    full_size: bool = False


PREVIOUS_DIR = "_previous"


def stash_previous(name):
    """Move aside the finished file a person made in an earlier session.

    A photo settled before this run has no outline recorded, so undo cannot
    rebuild it.  Cropping it again would therefore be the one action in this
    page with no way back, and the thing it destroys is the work we went to
    all this trouble to protect.  Keep the file instead of the recipe.
    """
    for bucket in parchi.BUCKETS:
        candidate = JOB["outroot"] / bucket / name
        if not candidate.exists():
            continue
        keep = JOB["outroot"] / PREVIOUS_DIR
        try:
            keep.mkdir(parents=True, exist_ok=True)
            shutil.move(str(candidate), str(keep / name))
            return bucket
        except OSError:
            return None
    return None


def remember_earlier_work(entry, name):
    """Give a settled photo something to undo to, the first time it is touched."""
    if entry.get("origin") or not entry.get("settled"):
        return
    bucket = stash_previous(name)
    if bucket:
        entry["origin"] = {"bucket": bucket, "quad": None, "stashed": True}


def clear_from_buckets(name):
    for bucket in parchi.BUCKETS:
        candidate = JOB["outroot"] / bucket / name
        if candidate.exists():
            try:
                candidate.unlink()
            except OSError:
                pass


@app.post("/api/crop")
def crop(req: CropRequest):
    """Apply the corners a person dragged, and file it as cropped."""
    with LOCK:
        entry = JOB["results"].get(req.name)
    if entry is None:
        raise HTTPException(404, "unknown photo")
    # usable_quad does the reshaping: doing it here first turns a request
    # with the wrong number of corners into a 500 instead of a clear refusal.
    points = np.array(req.quad, dtype="float32")
    bgr = parchi.turn(parchi.load(inside_root(entry["source"])), req.rotate)
    points = usable_quad(points, bgr.shape)

    result = parchi.warp(bgr, points, JOB["opts"]["margin"])
    result = parchi.finish(result, look(req))

    with LOCK:
        remember_earlier_work(entry, req.name)
    clear_from_buckets(req.name)
    outdir = JOB["outroot"] / "1_cropped"
    outdir.mkdir(parents=True, exist_ok=True)
    # full_size is the one per photo escape hatch: a parchi with very small or
    # very faint writing, saved at its original size whatever the run says.
    opts = dict(JOB["opts"])
    if req.full_size:
        opts["fit"] = 0
    parchi.put_image(result, outdir / req.name, opts)

    with LOCK:
        entry.update(bucket="1_cropped", quad=points.tolist(), edited=True,
                     score=100, settled=True)
    parchi.mark_decided(JOB["outroot"], req.name, "1_cropped")
    return {"ok": True}


@app.post("/api/keep")
def keep(req: KeepRequest):
    """Accept the photo as it is: the original, uncropped, marked done."""
    with LOCK:
        entry = JOB["results"].get(req.name)
    if entry is None:
        raise HTTPException(404, "unknown photo")
    with LOCK:
        remember_earlier_work(entry, req.name)
    clear_from_buckets(req.name)
    outdir = JOB["outroot"] / "1_cropped"
    outdir.mkdir(parents=True, exist_ok=True)
    opts = dict(JOB["opts"])
    if req.full_size:
        opts["fit"] = 0
    parchi.put_original(inside_root(entry["source"]), outdir / req.name, opts)
    with LOCK:
        entry.update(bucket="1_cropped", edited=True, score=100, settled=True)
    parchi.mark_decided(JOB["outroot"], req.name, "1_cropped")
    return {"ok": True}


# --------------------------------------------------------------------------
# page and lifecycle
# --------------------------------------------------------------------------

class UndoRequest(BaseModel):
    name: str


@app.post("/api/undo")
def undo(req: UndoRequest):
    """Put a photo back the way it was before someone cropped it.

    Cropping is one keypress, so mis-cropping is one keypress too.  Without a
    way back the only remedy is to find the original and start again, which is
    exactly when people stop trusting the tool and check everything twice.
    """
    with LOCK:
        entry = JOB["results"].get(req.name)
    if entry is None:
        raise HTTPException(404, "unknown photo")
    origin = entry.get("origin")
    if not origin:
        if entry.get("settled"):
            raise HTTPException(400, "This photo was finished by hand before "
                                     "this run, so there is nothing here to go "
                                     "back to. Crop it again, or start fresh.")
        raise HTTPException(400, "nothing to undo for that photo")

    clear_from_buckets(req.name)
    outdir = JOB["outroot"] / origin["bucket"]
    outdir.mkdir(parents=True, exist_ok=True)
    source = inside_root(entry["source"])

    if origin.get("stashed"):
        # the earlier session's own file, put back exactly as it was
        stashed = JOB["outroot"] / PREVIOUS_DIR / req.name
        if not stashed.exists():
            raise HTTPException(400, "the earlier version is no longer there")
        shutil.move(str(stashed), str(outdir / req.name))
        with LOCK:
            entry.update(bucket=origin["bucket"], quad=None, edited=True,
                         settled=True, origin=None)
        parchi.mark_decided(JOB["outroot"], req.name, origin["bucket"])
        return {"ok": True, "bucket": origin["bucket"]}

    if origin["bucket"] == "1_cropped" and origin["quad"]:
        points = np.array(origin["quad"], dtype="float32").reshape(4, 2)
        bgr = cached_image(req.name, source)
        result = parchi.warp(bgr, points, JOB["opts"]["margin"])
        # undo rebuilds the crop the RUN made, so the run's settings, not
        # whatever the operator had the sliders on a moment ago
        result = parchi.finish(result, JOB["opts"])
        parchi.put_image(result, outdir / req.name, JOB["opts"])
    else:
        parchi.put_original(source, outdir / req.name, JOB["opts"])

    with LOCK:
        entry.update(bucket=origin["bucket"], quad=origin["quad"], edited=False,
                     settled=False)
    # undo means undo: a later run may touch this photo again
    parchi.forget_decided(JOB["outroot"], req.name)
    return {"ok": True, "bucket": origin["bucket"]}


@app.get("/", response_class=HTMLResponse)
def index():
    return here("web", "index.html").read_text(encoding="utf-8")


@app.post("/api/shutdown")
def shutdown():
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return {"ok": True}


def port_is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex((HOST, port)) != 0


def hold_window():
    """Wait for a keypress, so a console that failed does not just vanish.

    Crop is started by double-clicking a .bat, which opens a window and closes
    it the moment the program ends.  Every message this program printed about
    WHY it could not start went with it: the operator saw a black window blink
    and had nothing to report but "it does not work".  parchi.py has had this
    since the beginning; cropui.py never got it.  Only on failure: a normal
    stop should close cleanly, and /api/shutdown calls os._exit anyway.
    """
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\nPress Enter to close this window...")
    except (EOFError, KeyboardInterrupt, ValueError):
        pass


def main():
    port = PORT
    if not port_is_free(port):
        print("Port {} is already in use.".format(port))
        print("")
        print("Crop is most likely already running: look for a window called")
        print("Crop-UI, or open http://{}:{} in your browser.".format(HOST, port))
        print("If you cannot find it, run STOP_Crop.bat and start again.")
        return 1
    url = "http://{}:{}".format(HOST, port)
    print("Crop is running at {}".format(url))
    print("")
    print("Leave this window open while you work.  This is the only one:")
    print("closing it stops Crop.  STOP_Crop.bat does the same.")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=port, log_level="warning",
                loop="asyncio", http="h11")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        code = 0                      # Ctrl-C is somebody stopping it on purpose
    except SystemExit as stop:
        # uvicorn exits this way when it cannot bind the port at all
        code = stop.code if isinstance(stop.code, int) else 1
    except Exception:
        import traceback
        traceback.print_exc()
        print("")
        print("Crop could not start.  The lines above say why.")
        print("If you are reporting this, a photo of this window is enough.")
        code = 1
    if code:
        hold_window()
    sys.exit(code)
