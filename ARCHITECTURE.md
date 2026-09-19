# AutoCrop: how it is put together

For picking the project up cold, in a fresh session or a fresh head.

This file is **what the code is**. `AGENTS.md` is **what we learned and must
not undo**: the defects, the measurements, the things that look like
improvements and are not. Read this one first, then that one before changing
detection or the audit.

---

## The one-paragraph version

Photographed handwritten bills go in, straightened and cropped images come
out. Detection proposes a quadrilateral, an evidence scorer decides whether to
trust it, and anything doubtful goes to a person on a local web page who drags
four corners. It runs entirely offline on Windows, packaged with PyInstaller,
and it never modifies or deletes an original.

The load-bearing idea: **these are bills, so a crop that slices off an amount
is far worse than no crop.** Every design decision follows from that.

---

## Files

```
parchi.py            1140 lines   detection engine + command line tool
cropui.py             787 lines   FastAPI server for the review page
web/index.html       1203 lines   the entire review page, vanilla JS
modelcand.py           53 lines   U-2-Net-p candidate generator, optional
pdfout.py              56 lines   img2pdf export, kept away from detection
autocrop.py                       unrelated: trims a plain even border
models/u2netp.onnx    4.6 MB      Apache 2.0, from the rembg release

selftest.py            21 checks  autocrop
parchi_selftest.py     34 checks  detection, generates its own photos
cropui_selftest.py     90 checks  drives the real server on port 8113

START_Crop.bat / STOP_Crop.bat    what the client runs
START_Crop_DEV.bat                runs from source, page edits live on refresh
build.bat                         venv, deps, all three suites, three exes
```

`web/index.html` is one file of vanilla JS **on purpose**. A build step would
put npm between this repo and a working exe, and the page is a folder picker,
a progress bar and four draggable circles.

---

## The path a photo takes

```
parchi.load(path)              Pillow first, exif_transpose, then OpenCV
   |                           (phone photos arrive sideways otherwise)
parchi.detect(bgr)             -> (quad | None, score 0..1)
   |    runs on a 1200px copy (WORK_SIZE), corners scaled back up
   |    five candidate generators, each proposing quadrilaterals:
   |      candidates_from_edges    Canny against the background
   |      candidates_from_paper    bright, low-saturation regions
   |      candidates_from_shadow   heavy blur, the shadow a sheet casts
   |      candidates_from_ink      a box round the handwriting, last resort
   |      modelcand                U-2-Net-p saliency mask
   |    every candidate scored by score_quad():
   |      shape_score   angles, opposite sides, aspect, fill, frame contact
   |      _edge_support does the outline sit on a gradient all the way round
   |      _inside_outside  is the inside brighter/greyer than a ring outside
   |    total = shape * (0.18 + 0.82 * evidence), capped at the frame edge
   |    _prefer_enclosing: a nearly-as-good candidate that WRAPS the winner
   |                       is the sheet (edge and paper candidates only)
parchi.decide(...)             -> (bucket, note)     THE ONLY PLACE THIS LIVES
   |    classify() turns score into a bucket against confidence/review
   |    audit_crop() vetoes a crop that would leave too much paper outside
   |    fills_frame() notes a close-up; a note never auto-files
parchi.warp(bgr, quad, margin)  perspective transform to a rectangle
parchi.turn(result, degrees)    quarter turns, on the RESULT not the corners
parchi.save(...)                into one of three folders
```

Output, beside the photos, never overwriting anything:

```
_parchi/1_cropped          done: straightened and cropped
_parchi/2_review           found something, not trusted, ORIGINAL copied
_parchi/3_not_detected     found nothing, ORIGINAL copied
_parchi/report.csv         one row per photo with its score
_parchi/_debug             outline drawn on the photo, with --debug
```

Folders 2 and 3 hold byte-identical copies. They are triage, not damage.

---

## The server

`cropui.py`, FastAPI on `127.0.0.1:8112` only. `cropui_selftest.py` uses 8113
so it never disturbs an open window.

State is one module-level dict:

```python
JOB = {"root", "outroot", "running", "done", "total", "log",
       "results",   # name -> {bucket, note, score, quad, width, height,
                    #          source, edited, origin}
       "opts"}      # confidence, review, margin, enhance, upright
LOCK = threading.Lock()      # the run thread and the request threads share JOB
```

Two caches, both keyed by photo name, both there because decoding a twelve
megapixel JPEG on every mouse move feels like treacle:

```python
_LAST         the decoded full-size image, for snap and crop
_PREVIEW_SRC  a scaled-down copy for the live preview, plus its scale factor
```

### Endpoints

| | |
| :--- | :--- |
| `GET /` | the page |
| `GET /api/browse` | server-side folder navigator |
| `POST /api/resolve` | turn a dropped or pasted path into a folder |
| `POST /api/pick` | native Windows folder dialog via PowerShell |
| `POST /api/run` | start a batch; validates confidence vs review |
| `GET /api/progress` | done/total and the log, polled |
| `GET /api/results` | every photo with bucket, score, quad, size |
| `GET /api/photo` | `kind=thumb\|full\|output` |
| `GET /api/edges` | small edge map, sent once per photo, for the live magnet |
| `POST /api/snap` | fit the outline to real edges, server side |
| `POST /api/preview` | the crop these corners would make, `size` and `rotate` |
| `POST /api/crop` | apply corners, file as cropped |
| `POST /api/keep` | accept the photo whole |
| `POST /api/undo` | put a photo back exactly where it was |
| `POST /api/shutdown` | what STOP_Crop.bat hits |

`/api/crop` and `/api/keep` write the photo's name into `_parchi/decided.csv`
through `parchi.mark_decided()`; `/api/undo` removes it again. `run_job()` and
`parchi.main()` both read that file before they start and skip every photo in
it, so a second run over the same folder cannot replace a person's crop with
the machine's. `redo` (the "Start fresh" tick box, `--redo` on the command
line) deletes the record and processes everything.

A photo settled in an earlier session has no stored outline, so before
re-cropping it `remember_earlier_work()` moves the existing file to
`_parchi/_previous/` and undo restores that file rather than rebuilding it.

Every served or written path goes through `inside_root()`. A local web server
with an unchecked path parameter reads any file on the machine.

`usable_quad()` owns reshaping and validation for snap, preview and crop. It
separates three failures that used to share one message: not four corners,
coordinates that are not numbers, and corners too close together.

`uvicorn.run(..., loop="asyncio", http="h11")` is pinned. Auto-discovery is
what breaks uvicorn inside a PyInstaller one-file exe.

---

## The page

One file, no framework, no build. Client state:

```js
results[]      every photo, mirroring JOB["results"]
queue[]  qi    photos awaiting a decision, and where we are in it
quad     turn  the four corners being dragged, and quarter turns wanted
picked         Set of names ticked in the bulk picker
imgBox         displayed vs original pixel sizes, for toView / toImg
edgeMap        the edge picture, for the client-side magnet
```

Four screens, top to bottom: choose a folder, settings, progress, then review.
Review holds three things:

**The picker.** Every photo still awaiting a decision as a thumbnail with a
tick. Flagged ones start ticked. Tick all / untick all / tick the likely
close-ups, an `open` on each tile, and one button that keeps all the ticked
ones. On the real parchis this turns 25 trips through the editor into one
click. `keepPicked()` removes a photo from the queue only when its save
actually succeeded.

**The editor.** Two co-equal panes, `Original photo` and `What you will get`,
the second updating as corners move (`/api/preview`, debounced 180ms). A
narrow side panel of buttons, each carrying its shortcut. Keys: `C` crop,
`K` keep whole, `S` skip, `M` snap, `R` rotate, arrows nudge.

**The comparison.** Clicking any finished photo opens the original beside the
saved crop full screen, arrow keys to step through, and buttons to crop again
or undo.

Two magnets, doing different jobs. `snap_to_edges` on the server is a
coarse-to-fine line fit run when a drag ends. The client-side `magnet()` uses
the edge map fetched once per photo so a dragged corner sticks instantly,
without a round trip per mouse move.

---

## Invariants

1. Originals are never modified, renamed or deleted.
2. `parchi.decide()` is the only place the bucket rule lives, so the command
   line tool and the page cannot drift apart.
3. `AUDIT_LEAK_LIMIT` is used in exactly two places, the veto and the report.
   One number, or the tool can ship a crop its own audit rejects.
4. Every path through `inside_root()`.
5. A close-up note never files a photo on its own; it only suggests.
6. Rotation acts on the warped result, never on corner order. `warp()`
   normalises corner order, so a re-ordered quad is silently undone.
7. The picker is built from the whole remaining queue, not the flagged subset.
8. `build.bat` refuses to build if any suite fails.
9. Every file the tool writes goes through `put_image()` or
   `put_original()`, so the size setting cannot miss a path. NOT through
   `finish()`, which `/api/preview` also calls on an already-scaled copy.
10. `collect()` skips `_smaller` as it skips `_parchi`: a second pass reads
   the originals, never its own output.
11. A person's decision beats the machine's. Nothing may overwrite a file named
   in `decided.csv` except a run the person explicitly started fresh.

---

## Running it

```
python cropui.py                  the page, port 8112
START_Crop_DEV.bat                the same, from source, on Windows
python parchi.py <folder> --debug green confident, orange review, red none
python parchi.py <folder> --audit check every crop for paper left outside

python selftest.py                21 checks
python parchi_selftest.py         34 checks, draws its own photos
python cropui_selftest.py         90 checks, real server on 8113
```

Building needs Windows: `build.bat`. The exe bundles `web/`, so **a page
change does not reach the client until the exe is rebuilt.**

---

## Where the bodies are buried

Read `AGENTS.md` before changing detection, the audit, or the paper mask. The
short version:

- Shape plausibility alone is useless as confidence. The outline of the whole
  photo is also a perfect rectangle.
- `audit_crop`'s paper mask calls a patterned bedsheet paper. On real parchis
  its blob averages 81% of the frame.
- Twice now, a number that looked good was measuring something else. Both bugs
  were found because a result looked too good, not too bad.
- If you find yourself choosing a constant that separates two named files,
  change the mechanism, not the number.
- A message that blames the user is a claim, and needs the same evidence as
  any other claim. Two shipped bugs read as operator error on screen.

## Client data

`SAMPLES/`, `From_Client/` and `REAL_SAMPLES/` are gitignored. The repo is
public and the client is particular about privacy. Never commit a photo.
