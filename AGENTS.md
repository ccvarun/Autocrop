# Agent brief: Autocrop / parchi

Standing context for any agent working in this repo. Read this before changing code.

## What this is

Two portable offline Windows tools, drag-and-drop console exes built with
PyInstaller. No GUI, no installer, no network at runtime.

| File | Job |
| --- | --- |
| `autocrop.py` | Trim a plain even border off an image. Pillow only. |
| `parchi.py` | Find a paper slip in a phone photo, straighten it, crop it. OpenCV. |
| `selftest.py` | 21 checks for autocrop. |
| `parchi_selftest.py` | 16 checks for parchi. Generates its own synthetic photos. |
| `cropui.py` | Local review page: FastAPI on 127.0.0.1:8112, served to a browser. |
| `web/index.html` | The whole page. Vanilla JS on purpose, see below. |
| `cropui_selftest.py` | 22 checks: drives the real server on a spare port. |
| `START_Crop.bat` / `STOP_Crop.bat` | Start and stop the page, matching the workspace convention. |
| `build.bat` | venv, install, run all three suites, build three exes. Windows only. |

## The real requirement

Handwritten bills ("parchiyan") photographed on phones by staff at hundreds of
different centres. Uncontrolled input: any background, angle, lighting, camera.
There is no fixed format, no controlled capture, no training data.

These are financial documents. A crop that slices the total off the bottom of a
slip is far worse than no crop at all. Every design decision follows from that.

## Invariants: do not break these

1. Nothing is destroyed. Originals are never modified, moved or overwritten.
   Undetected photos are copied through byte for byte.
2. Three output buckets, always: `1_cropped`, `2_review`, `3_not_detected`.
   Doubt goes to a person, never to a guess.  Both `2_review` and
   `3_not_detected` copy the original byte for byte; the difference is triage:
   `2_review` means "detection found something, see report.csv and --debug",
   `3_not_detected` means "nothing found".
3. Confidence must mean something. A photo only reaches `1_cropped` when the
   detected outline sits on real image evidence, not merely a plausible shape.
4. Paper running off the frame edge can never be a confident crop. Part of the
   bill is missing.
5. `autocrop` and `parchi` stay drag-and-drop console exes. No config file, no
   network call, no telemetry. `cropui` binds to 127.0.0.1 only and never
   reaches the internet.
6. `parchi.classify()` is the only place the bucket rule lives. The command
   line tool and the page both call it so they cannot drift apart.
7. Every path the page serves or writes goes through `inside_root()`. A local
   web server with an unchecked path parameter reads any file on the machine.
8. `web/index.html` is deliberately one file of vanilla JS with no build step.
   A Vite build would put npm between this repo and a working exe, and this
   page is a folder picker, a progress bar and four draggable circles.
9. A browser will not tell a page where a dropped folder lives. Windows
   Explorer usually also puts the path into the drag as `text/uri-list` or
   `text/plain`, and `/api/resolve` turns that into a real folder. When it is
   absent, say so and offer `/api/pick`, which opens the machine's own folder
   chooser through PowerShell: the modern IFileOpenDialog with FOS_PICKFOLDERS,
   falling back on its own to the old FolderBrowserDialog if the interop fails.
   The response says which one opened, in `style`, so a support question can be
   answered without guessing. Never guess at which folder was meant by
   searching the disk for a matching name: on a bill archive, opening the
   wrong folder is not a harmless mistake.
10. `cropui_selftest.py` must never call `/api/pick` on Windows. It opens a
   modal dialog and the test would hang until someone clicks it.
6. Every option error prints a readable message and still pauses. A traceback
   in a drag-and-drop tool means the window closes before anyone can read it.
7. `opencv-python-headless` only. Plain `opencv-python` drags in Qt and doubles
   the exe.

## How parchi decides

Three independent candidate generators propose quadrilaterals:
`candidates_from_edges` (Canny against the background), `candidates_from_paper`
(bright low-saturation regions), `candidates_from_ink` (weak last resort, a box
around the handwriting), and `candidates_from_shadow` (soft drop-shadow gradient
cast by the sheet, for white-on-white scenes).

Each candidate is scored on two separate axes, then multiplied:

```
total = shape_score * (0.18 + 0.82 * evidence)
evidence = 0.62 * edge_support + 0.38 * inside_outside_contrast
```

- `shape_score`: corners near 90 degrees, opposite sides equal, aspect ratio
  under 5:1, filling at least 10 percent of the frame, not hugging the frame edge.
- `_edge_support`: fraction of the outline actually lying on a brightness
  gradient. The weakest of the four sides is weighted double.
- `_inside_outside`: brightness and saturation just inside the outline versus a
  ring just outside it.
- `_prefer_enclosing`: when a nearly-as-good candidate wraps around the winner,
  the wrapper is the sheet and the winner was something printed on it.

Thresholds: `--confidence` default 62, `--review` default 35, both out of 100.

## Hard-won findings: do not undo these

- Shape plausibility alone is worthless as a confidence signal. The first
  version scored only shape and called all 15 test photos confident, including
  ones off by 13 to 19 percent, because the outline of the whole photo is also
  a perfect rectangle. Adding pixel evidence took mean corner error on
  confident crops from 7.5 percent to 0.6 percent.
- Bills have boxes and tables printed on them. A printed box is a cleaner
  rectangle than the torn edge of the paper around it, and detection locked
  onto a header strip instead of the slip. That is why the minimum fill, the
  5:1 aspect rejection and `_prefer_enclosing` exist.
- Detection runs on a 1200px copy (`WORK_SIZE`) with corners scaled back, so
  output stays full resolution. Do not detect at full resolution, it is slower
  and noisier.
- Photos are opened through Pillow with `exif_transpose` before OpenCV sees
  them. Without it phone photos are processed sideways.
- Shadow-based detection blurs at σ=20 before gradient extraction because the
  drop shadow is a slow gradient invisible to Canny.  The detected contour lies
  outside the paper edge (the shadow falls outside), so two inward-shifted
  variants (10% and 12% of the image's longer dimension) are returned.  On
  synthetic test images the maximum shadow score is ~55%, safely below the 62%
  confidence threshold, so shadow quads reach 2_review but never 1_cropped.
- Shadow and ink candidates must not feed `_prefer_enclosing`.  A large shadow
  quad wraps around the correct paper detection and can be mistaken for "the
  sheet containing the winner", replacing a precise detection with an imprecise
  one.  Only `edge` and `paper` candidates go into `scored_for_enclosing`.
- The edge-support gradient threshold (`_edge_support`) is relative to the
  98th-percentile gradient in the image.  On real receipts with dense print
  the peak gradient is dominated by text edges, not the paper boundary.
  The threshold was lowered from 0.18 × peak to 0.12 × peak on evidence from
  200 real photos.  This took cropped-outright from 32 to 42 with 0 audit
  failures.  Do not raise it back without re-running the benchmark.
- A hard cap in `score_quad` prevents confident crops when more than 12.5%
  of the paper mask sits outside the outline.  The threshold is tight:
  the highest leak on a correct crop in the sample is 12.1%, the lowest
  audit failure is 12.6%.  Revisit on the real parchis.

## Everything in the editor exists to lower the cost of one photo

Detection is at about 13.5 percent auto-cropped on the 200 real photos, so
almost all the human time in this tool is spent in the review editor. Work
there pays off roughly seven times more than the same effort spent on
detection. In rough order of what each saved:

- Whole-edge dragging: one drag instead of matching two corners by eye.
- The live magnet: place a corner roughly, it lands exactly.
- The loupe: your hand and the handle both hide the pixel you are aiming at.
- Arrow-key nudge, for the last pixel a mouse cannot give you.
- Close-up sweep: one button clears a whole group.
- Undo, so a mis-crop costs one click rather than a restart.

## Two magnets, and they do different jobs

`/api/edges` sends a small greyscale edge picture once per photo, and the page
snaps a dragged corner to the brightest nearby pixel with no network call. That
is what makes it feel magnetic: asking the server per mouse move is a round trip
per pixel and lands the outline a moment after the finger, which reads as broken
rather than as help.

The edge map deliberately includes the wide, soft gradient of a drop shadow as
well as the sharp step of a lit edge. On a white slip on a white desk the shadow
is the only edge there is.

`snap_to_edges` still runs on release. It fits a line along each whole side and
intersects them, which is more accurate than any per-pixel snap but far too slow
to run per mouse move. Keep both: the cheap one for feel, the careful one for
the final placement.

Whole sides are draggable (`.sidegrab` lines). When one edge of a slip is off,
moving that edge is one drag instead of two corners.

## The magnet

`parchi.snap_to_edges()` pulls a roughly placed quadrilateral onto the real
paper edges, and `/api/snap` is what the review page calls when a handle is
let go. It fits a line to each side from many samples and intersects
neighbours for the corners, rather than snapping a corner to the nearest edge
pixel: one pixel of grain or a fold cannot then drag a whole side.

It runs coarse to fine (`bands=(5.0, 2.0, 1.0)` percent of the frame). Both
halves matter. A person drops a corner roughly, often a hundred pixels out, so
the first sweep has to see far enough to reach the edge at all; a single wide
sweep would happily lock onto the edge of the table instead, which is what the
narrow sweeps afterwards prevent. The first version used one 2 percent band
and silently refused to move anything a person had actually dragged.

Measured on the synthetic set: a 2.5 percent jitter improves from 1.35 to 0.56
percent mean corner error, a 7 percent rough placement from 3.63 to 1.29
percent, and nothing is made meaningfully worse. Keep that measurement in the
loop when changing any of it.

## What the first real photos showed

30 real receipt photos in `SAMPLES/Parchis`. The synthetic set had flattered
the tool badly, and two things came out of running it on real data.

**Confident crops were cutting bills.** Two of seven cut into the paper, one
losing half of it. The printed block of text on a receipt is a cleaner
rectangle than the torn, shadowed edge around it, so detection locked onto the
text and sliced off the last lines. `_paper_left_outside()` now measures how
much paper-like pixel area falls outside a candidate and penalises it. That
took confident crops from 5 of 7 clean to 5 of 5 clean, and it is a
generalisation of what `_prefer_enclosing` was doing by hand.

**Most "failures" were close-ups.** Photographers fill the viewfinder with the
bill, so there is no paper boundary anywhere and nothing to crop. Those photos
are finished work being reported as failures. `fills_frame()` spots them by
comparing a ring around the edge of the photo against the middle.

`fills_frame()` deliberately does NOT file a photo as finished. A white slip on
a white desk has a border matching its middle too, and is indistinguishable
from a close-up by every measure tried: edge support, inside/outside contrast,
large-scale gradient ridges, paper-mask coverage. All of them overlap. Filing
on that basis would silently skip a bill that needed cropping, which is the one
failure this whole design exists to prevent. It flags instead, the page sorts
flagged photos last, and one button clears the group after a person has looked
at the thumbnails.

Numbers on those 30 photos: 5 cropped outright and correct, 9 flagged as
close-ups (one click), 16 needing real attention. Do not quote a headline
accuracy figure from this: these are American till receipts, long and thin and
often crumpled, standing in for handwritten parchis nobody has sent yet.

Numbers on 200 real receipt photos (`SAMPLES/PARCHI_SAMPLES`): 42 cropped
outright with 0 audit failures, 46 flagged as close-ups, 93 review (real
work), 19 not detected.  The edge threshold and hard leak cap tuning is
fitted against this batch; see `SAMPLES/baseline.csv` (before) and
`SAMPLES/after_edge_fix_v3.csv` (after).

## Checking a large batch

`parchi.py <folder> --audit` re-measures every finished crop for paper left
outside it, names the suspicious ones and exits non-zero. It is deliberately a
second opinion run after the decision rather than part of the score: the scorer
can be wrong, and on the first 30 real photos it was, on 2 of 7 crops.

Use it on any new batch instead of looking through hundreds of photos. Look at
the ones it names, plus a handful of others in the page's side-by-side preview.

`cropui`'s `/api/preview` renders the crop the current corners would produce,
live, next to the photo. `usable_quad()` rejects a collapsed outline in both
preview and crop: without it a degenerate quad quietly writes a ten pixel image
and files it as a finished bill.

## Feedback after a crop, and undo

Cropping is one keypress, so mis-cropping is one keypress too. `/api/undo`
restores a photo to the bucket and outline it had before a person touched it,
using the `origin` recorded on each entry at run time.

The page shows finished photos in a "Just done" strip with the saved crop and
an Undo, rather than a confirm step after every crop. A blocking confirmation
would double the clicks on the one screen built for speed, and the live preview
already answers "what will I get" before committing. What was missing was a way
back afterwards, not another checkpoint before.

## Before and after, at the same size

The editor is two co-equal panes: the photo on the left with the draggable
outline, the crop that outline would produce on the right, labelled `Original
photo` and `What you will get`. It used to be a 300px box in the 240px side
panel. A thumbnail cannot answer "did that cut the total off the bottom?", and
that is the only question the operator is actually being asked, so the answer
gets the same amount of screen as the question.

Consequences that are easy to undo by accident:

- `/api/preview` takes a `size`, and the page asks for one as large as the
  photo beside it (`max(shot.clientWidth, shot.clientHeight) * dpr * 1.1`).
  A preview smaller than the photo makes the two panes incomparable, which
  defeats the point of the layout.
- `/api/preview` warps a **scaled-down copy** of the photo (`preview_source`),
  not the full 12 megapixels. On a real phone photo that is 24 ms instead of
  92 ms, so the bigger preview is also the faster one. `/api/crop` still warps
  the full-resolution original; do not let these two converge.
  `cropui_selftest.py` checks that the preview is the same **shape** as the
  crop that gets saved, which is what stops a scaled preview from quietly
  drifting away from the real result.
- `.stage` is a named-area grid. Side by side is the default; the
  `Bigger photo, result below` button (`crop.layout` in localStorage) switches
  to one wide photo with the result underneath, for corners that need placing
  to the pixel. Below 1180px wide it stacks regardless. On a wide monitor
  `#step-review` is given negative margins so the two panes can spill past the
  page's 1180px reading width; nothing else on the page does that.
- Both panes cap at `74vh` so they stay on screen together. In wide mode the
  photo goes to `86vh` and the bordered box hugs it (`width:fit-content`),
  otherwise a tall photo sits in a field of grey.

## Comparing a crop after the fact

Clicking a photo in "Everything in this folder" or in the "Just done" strip
opens a full-screen overlay with the original and the saved crop side by side:
`/api/photo?kind=full` against `/api/photo?kind=output`, the latter always
cache-busted because a crop can have been rewritten a second ago. Arrow keys
step through the cropped photos, Esc closes, and the overlay offers `Crop this
one again` and `Undo the crop`.

- Clicking a photo that is **not** yet in `1_cropped` still goes straight to
  the editor, as it always did. There is nothing to compare it against.
- The keydown handler returns early while the overlay is open. Without that,
  `C` and `K` fire on the photo behind it and crop something the operator is
  not looking at.
- `doCrop` now records the corners it used on the entry, so `Crop this one
  again` resumes from where the operator left off rather than from detection.
- Undo lives in one place (`undoCrop`) because the strip and the overlay must
  do exactly the same thing.

## THE AUDIT DOES NOT WORK. FIX IT BEFORE TRUSTING ANY NUMBER

`audit_crop` cannot detect the failure it exists to detect. Its paper mask
thresholds brightness at the 62nd percentile of the frame, so it always selects
roughly the brightest 38 percent of the photo whatever is in it, and therefore
can never report "most of the paper is outside the crop".

Demonstrated: on 1071-receipt.jpg the classic crop starts at y=198 on a
1000px-tall photo and throws away the receipt's entire header. `audit_crop`
returns 3 percent. On 1160 it clips the bottom edge and returns 2 percent.
A rewrite using colour similarity to the crop's own interior as the reference
failed the same photos, so the fix is not obvious and was reverted.

Everything resting on this measure is therefore unproven, including "0 audit
failures on 200 photos" and the leak penalty in the scorer, which uses the same
mask via `_paper_left_outside`.

Fix it test-first, in this order:
1. Build a test that FAILS on the current code: take real photos with a known
   good crop, deliberately cut 20 percent off one side, and assert the audit
   flags it. Do this for several photos and several sides. It should fail today
   on most of them.
2. Only then change the measure, until that test passes and correct crops still
   pass.
3. Only then re-run the 200 and quote numbers again.

Do not tune thresholds against this measure until step 2 is done.

## The audit is a veto, not a report

`AUDIT_LEAK_LIMIT` is one number used in exactly two places: the veto at the
end of `decide()`, and the `--audit` report. Keep it that way. If the two ever
differ, the tool can ship a crop its own audit rejects, and on a big enough
batch it will.

The veto measures the crop that would actually be produced and can only demote,
never promote, so the scorer still decides and this cannot be gamed into
approving anything.

A hard leak cap inside the scorer was tried and removed. It was set at 12.5
percent, chosen to sit between a correct crop at 12.1 and a wrong one at 12.6
in a 200-photo sample. Two photos half a percentage point apart cannot define a
threshold: on that same sample four correct crops sat at 11.8 to 11.9, so the
whole thing balanced on a tenth of a point. It also sat above the audit's own
limit, which is the bug described above. If you find yourself picking a
constant that separates two named files, that is the signal to change the
mechanism, not the number.

## Rules for changing detection

- Every behaviour change needs a case in `parchi_selftest.py`.
- Never loosen a threshold to make a test pass. If a test fails, either the
  detector got worse or the test encodes a requirement you disagree with. Say
  which, do not quietly retune.
- The assertion that matters is: when parchi says confident, it is right.
  Recall is negotiable, precision is not.
- Report detection rate, never assert it. Only real photos can establish that.

## Running things

```
python selftest.py
python parchi_selftest.py            # add --keep to leave the photos on disk
python cropui_selftest.py            # starts the real server on port 8113 (66 checks)
python parchi.py <folder> --debug    # green confident, orange review, red none
python cropui.py                     # the page, on port 8112
```

`_parchi/report.csv` gives one row per photo with its confidence, which is how
two runs get compared after a tuning change.

Building requires Windows: double-click `build.bat`. It refuses to build if
either suite fails.

## Backlog, in priority order

1. ~~White slip on a white desk currently fails even when the drop shadow is
   clearly visible to a human eye.~~ Done: `candidates_from_shadow` added.
   White-on-white now reaches 2_review on 4/4 synthetic test photos.  Shadow
   quads are never confident: their scores cap at ~55% on the test suite.
2. Run against the real sample batch from the centres when it arrives, tune
   `--confidence` from the debug overlays, and record the true hit rate.
3. Verify the PyInstaller build on Windows and check the exe start-up time on a
   typical centre machine.
4. Crumpled or folded slips have no straight quadrilateral. Currently they land
   in review, which is acceptable. Only worth attacking if they turn out to be
   common in the real batch.
5. Rotate-to-portrait on the result, and a "N left, about Y minutes" pace
   indicator. Both are editor-cost work, which is where the remaining 86% of
   the effort actually sits.

## Open question the client has not answered

What happens to the parchis after cropping. If the endgame is data entry or
OCR, legibility matters more than tight edges and the preprocessing should be
built differently. Do not build OCR or enhancement work on assumption.
