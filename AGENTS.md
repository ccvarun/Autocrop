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

The tick box is ON at the start of every sitting, and that is deliberately NOT
remembered between them.  It used to be stored in localStorage, so one
accidental click left every later session placing corners worse, with nothing
on screen to say why, on a machine nobody here can look at.  Turning it off
still lasts the whole sitting, which is what somebody who means it actually
needs; reloading the page brings it back.  `let magnetOn = true` holds it, and
the markup carries `checked` too so it is on in the first painted frame rather
than a moment later.  Do not "restore" the stored setting: the asymmetry is
the point, because the cost of it being wrongly off is invisible and the cost
of it being wrongly on is one click.


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

## What the first client run found, and the shape of both bugs

Two faults, both reported from a client machine on real parchis, and both the
same mistake underneath: **the page blamed the person for something it had
done itself.**

- The confidence slider ran from 20 to 95 while the page pinned the review
  threshold at 35 and the server refuses `review > confidence`. So anyone who
  followed the hint printed directly above the slider ("lower the confidence
  if too many photos are being sent for checking") was stopped with
  `{"detail":"review threshold cannot be above confidence"}` shown raw in an
  alert box. The review threshold is now derived, `min(35, max(5, confidence
  - 10))`, which is still exactly 35 at the default 62. `cropui_selftest.py`
  now reads the slider's `min`/`max` and the page's own formula out of the
  served HTML and checks every setting in between is one the server accepts,
  so the two can no longer drift apart.
- `/api/preview` refuses for at least four different reasons and the page
  printed "the four corners overlap" for every one of them, including plain
  server errors. That is the single screen that could have said what went
  wrong, and it was pointing at the operator. It now shows the server's
  reason, logs status and quad to the console, and separates "corners too
  close together" from "the outline lost its position, this is a bug, not
  something you did".

Hardening that came out of chasing the second one:

- `/api/snap` validated nothing in either direction. Two fitted sides that
  come out nearly parallel intersect a very long way off, so the edge fit can
  return an unusable outline; the page adopted it without looking, and every
  preview from then on failed as "the four corners overlap". Snap now refuses
  to return a quad it would not accept back, and the page refuses to adopt
  one. The magnet is on by default, so this sat in the normal path.
- `usable_quad` gave the same sentence for a collapsed quad, a sliver, and a
  NaN. NaN is worth separating: every comparison against it is False, so a
  non-finite corner fell straight through the area and side checks and came
  out reported as the operator's placement.
- A photo that loaded fine but failed during detection was recorded with
  width and height of zero, which folds all four corners onto one point. It
  now keeps the photo's real size, and the editor refuses to open on a photo
  it cannot measure rather than showing a dead screen.
- Three endpoints returned 500 on a malformed quad because they reshaped
  before validating. `usable_quad` owns the reshape now.

The general rule, worth keeping: **a message that blames the user is a claim,
and it needs the same evidence as any other claim.** Both of these looked like
operator error on screen and were ours in the code, and the misleading copy is
what made them expensive to find.

## THE FIRST REAL PARCHIS, AND WHAT THEY BROKE

26 real photos arrived at `SAMPLES/REAL_SAMPLES`. Every number this project
had before them describes American thermal till receipts. Read this before
touching detection.

What they actually are: 2592x1936, all landscape, shot on a **patterned
green-and-gold fabric**, and each photo is **not one sheet**. It is a stack of
overlapping slips: a handwritten Hindi note plus its supporting printed
receipts (CNG cash memo, railway platform ticket, HP oil company bill), laid
on top of each other. The thing to crop is the group, and a group of
overlapping papers is not a quadrilateral.

First measurement, at the default 62/35:

```
cropped outright                  1
close-up flagged                 10
demoted by the audit veto         7
review                            7
not detected                      1
needing a person                 25 of 26
```

**The audit is broken again, in a new way, and it is the reason for 7 of
those.** `audit_crop` builds its paper mask as `(sat < 90) & adaptiveThreshold`.
The pale parts of that fabric pass both tests. Measured over all 26 photos,
the largest "paper" component averages **81% of the whole frame**, and is over
70% of the frame on **23 of 26**. So on real parchis the audit is not
measuring "how much of the bill did this crop leave outside". It is measuring
roughly "how tight is this crop", which means **it vetoes the good crops**.
The seven it demoted were the seven highest-scoring detections, and several of
their crops are correct and complete when you look at them.

This is the third time a number in this project looked fine while measuring
something else, and the second time it was this function. The mask is the part
to distrust, not the arithmetic around it.

**CORRECTION, and it matters.** On first reading those numbers I concluded the
audit was "vetoing the good crops". That was wrong, and the client put it
right: in **25 of these 26 photos the parchis run off the edge of the frame**.
Only IMG_5821 has bedsheet all the way around it. So there is nothing to crop
in 25 of them, a tighter crop really would cut paper, and the seven vetoes were
correct in outcome even though the mask that produced them was partly measuring
bedsheet. The mask defect is real; the conclusion drawn from it was not.

Read the headline number the other way round: the tool cropped **1 of 1
croppable photos, correctly** (the client confirmed that outline by eye), not
1 of 26.

**So the job on this data is not cropping. It is saying "nothing to crop"
quickly, 25 times out of 26.** That makes `fills_frame` and the close-up sweep
the features that matter here, not detection accuracy. `fills_frame` currently
fires on 10 of 26; it should fire on about 25.

Tried, measured, and **deliberately not shipped as an automatic rule**:
deciding it by how many frame edges the paper mask reaches. Run over 26 real
parchis and 54 receipts:

```
                          fires on      wrongly, on photos the tool cropped
parchi   >= 2 edges        23 / 26                0 of 1
receipt  >= 2 edges        38 / 54                0 of 5
```

It never contradicts a successful automatic crop. That sounds like a pass, and
it is not one: across 80 photos only **6** were cropped automatically, so the
ground truth is far too thin to certify a rule whose failure mode is silently
skipping a photo that needed cropping. (An earlier read of 6 receipts called
two of them failures; that judgement was itself an assumption, not a
measurement, and the wider run does not support it.)

**So the uncertain signal is used to suggest, never to decide.** See the next
section: the operator ticks thumbnails, and the tool only chooses which ones
start ticked. A wrong suggestion costs one click. A wrong automatic decision
costs a bill.

## The paper mask: a fix that made things worse, with the numbers

`audit_crop` builds its own mask, `(sat < 90) & adaptiveThreshold`, and that
mask calls a printed bedsheet paper. It is genuinely wrong. **It was still
reverted, twice, and here is the evidence so nobody spends another evening on
it.**

Adding a smoothness gate (paper is smooth, a printed bedsheet is not) does
exactly what it promises: on the 26 real parchis the paper blob drops from 81%
of the frame to 69%, and the over-70% cases from 23 to 15. Cropped went 1 to 2.

**That second crop was wrong.** IMG_5831 has a pink slip at the left edge, and
the crop slices through it. The mask missed the pink slip because pink is
saturated and the gate is `sat < 90`, so a coloured document is not paper. The
audit's leak fell to 0.035 and the veto stopped firing. A better-looking
number, a shipped crop that cuts a bill.

The obvious correction does not work either. Loosening the saturation gate:

```
sat_max   IMG_5831 leak   IMG_5821 leak   blob on 5821
  90          0.035          0.010          58% of frame   5831 SHIPS A CUT
 130          0.130          0.168          71% of frame   5831 vetoed, 5821 ALSO vetoed
 170          0.132          0.205          75% of frame   same
 255          0.132          0.205          75% of frame   same
```

At 90 it ships the cut. At 130 and above it vetoes IMG_5821, the one photo in
the set that genuinely is croppable and whose outline the client confirmed by
eye. There is no value in between.

**That is a constant separating two named files, so the mechanism is wrong,
not the number** (see the rule further up this file, earned the same way). One
saturation gate is being asked to do two opposing jobs at once: reject a
saturated bedsheet, and accept saturated slips. Texture is the only thing that
can tell those apart, and a single-scale standard deviation over 1.2% of the
short side does not.

If you pick this up: the next mechanism to try is not another threshold. It is
something that uses *shape or connectivity* rather than colour alone, for
example that the bedsheet is one sprawling region touching the border while
slips are compact and bounded. Measure it against both IMG_5821 and IMG_5831
before believing anything, and against the receipts, which the current mask
handles correctly.

The helpers written for this (`paper_mask`, `largest_paper`,
`paper_runs_off_frame`) were **deleted rather than left unused**. Dead code
carrying a known defect is an invitation.

## Brightness and contrast, and the one function that fixed a quiet lie

The client asked for brightness and contrast. `parchi.adjust()` does it
linearly, and **pivots on mid grey**: scaling straight from zero means every
contrast change also shifts the brightness, so the two sliders fight and
nobody can get where they are going.

Both live in two places: the batch Settings, which apply to the automatic
crops, and the editor, where they start from the batch value and change only
the photo in front of you, live in the After pane. `Reset both` goes back to
the batch value, not to zero.

**The important part is `parchi.finish()`.** Everything done to a crop after
the warp now goes through it: the batch run, `/api/preview`, `/api/crop`,
`/api/undo` and the command line tool. Before it existed, `/api/preview`
skipped the contrast lift that `/api/crop` applied, so the pane labelled "what
you will get" was **quietly wrong every time the enhance box was ticked**, and
had been for as long as that pane existed. Nobody would have noticed from the
outside. Same rule as `decide()`: one place, or two paths drift and it takes a
month to find out. `cropui_selftest.py` now asserts `enhance_image` appears
nowhere in `cropui.py`, so no endpoint can reach past `finish()` again.

Measured honesty of the After pane, preview against the file that gets saved,
as mean pixel difference:

```
brightness and contrast only     3.3 to 4.7   (resampling and JPEG only)
with the adaptive lift on        6.6 to 11.3  (bias about -3 to -5)
```

The linear adjustment is exact, because it is scale-invariant. The adaptive
lift is not: the preview warps a scaled copy, and CLAHE on a scaled image is
not the same as CLAHE on the full one, so the preview comes out slightly
brighter than the file. It is close enough to judge "can I read the pen" and
it is not pixel-exact. Do not try to fix it by running CLAHE on the full
resolution image in the preview path: that is the 92ms warp we deliberately
removed, on every drag.

**A test lesson worth keeping.** The first assertions said the mean must move
by exactly the slider value. That holds on a mid grey patch and fails on a
real crop, because a bill is mostly bright paper and clips at 255. The test
was wrong, not the code. The assertions now check the property that is always
true, that it moves and only ever in the direction asked, and the exact
arithmetic is checked separately on a synthetic patch with headroom.

## Trimming the background off the sides that show it

The client's correction: "that small border needs to be trimmed". They are
right, and "nothing to crop" was the wrong phrase. There is nothing to crop
**as a four-corner rectangle**, because the corners are outside the picture.
There is still background on the sides that do show, and until now those
photos went to `2_review` and were copied **untouched**, strips and all.

`parchi.trim_to_frame()` is the other half of the job: an axis-aligned box
around whatever detection found, with any side already near the frame pushed
out TO the frame. Two properties make it safe to offer. It is never smaller
than the box around the detected outline, so it cannot cut anything that
outline would not have cut. And on a side where the paper runs off the photo
it removes nothing. No perspective correction, and there cannot be: you cannot
straighten a sheet whose corners were never photographed.

Measured on the 26 real parchis it removes a **median 30% of the frame**, up
to 37%.

**It is offered, never applied.** A button in the editor, labelled with the
exact percentage it will take ("Square off, trim 35% background"), shown only
when that is at least 5%. The reason is in the pictures: of six trims examined
by eye the main slip survived every time, but four clipped a *second* document,
a perforated platform ticket along the bottom or a coloured memo at the side.
The trim inherits detection's error. With a person confirming, that is a good
starting position; unsupervised it would quietly shave tickets off bills.

**A gate was tried and removed.** Opening on the trim automatically when the
outline touches two or more frame edges sounds right and is not:
`quad_touches_frame` reads where the OUTLINE sits, and detection puts its
outline inside the frame, so the photos that would gain 30 to 37 percent read
zero. The gate fired on 4 of 26, and not the right 4. Choosing this for the
operator needs a reliable read of whether the paper runs off the frame, which
is the paper mask, which is the part of this tool that cannot be trusted.
`usingTrim = false` on open, deliberately: see the comment in `showNext`.

The audit cannot referee any of this. It flagged 15 of the 26 trims as cutting
a bill, because its mask counts bedsheet as paper and so reads "removed
background" as "cut a document".

**And the real fix is upstream.** One instruction to the centres, "leave a
margin of background all round and one set of slips per photo", would take 25
of these 26 from uncroppable to croppable. IMG_5821 is the proof: same
bedsheet, same phone, same person, and it worked because there was a margin.
No algorithm recovers a corner that was never photographed.

## Clearing many photos at once

On the real parchis, 25 of 26 need "keep as it is". Walking those through a
four-corner editor one at a time is the actual cost, and no detector was going
to remove it, because the tool cannot reliably tell a close-up from a
white-on-white failure by colour and texture. **A person can tell instantly
from a thumbnail.** So the sweep became a picker.

Every photo still awaiting a decision appears as a thumbnail with a tick.
Photos the tool put a note on start ticked; everything else starts clear.
`Tick all`, `Untick all`, `Tick the likely close-ups`, and an `open` button on
each tile that sends that one to the editor instead. Then one button keeps all
the ticked ones as they are.

Measured on the 26 real parchis: 25 to check becomes one glance and one click,
with 7 left over. Before this it was 25 trips through the editor.

Two invariants in `keepPicked`, do not lose them:

- Only photos whose `/api/keep` actually succeeded leave the queue. Filtering
  the queue optimistically would make a failed save look exactly like a
  successful one, which is how photos would go missing silently.
- The picker is built from `queue.slice(qi)`, the whole remaining queue, not
  from the flagged subset. On the real parchis the notes covered 17 of 25, and
  the operator could clear all 25. `cropui_selftest.py` checks that line is
  still there, because narrowing it back to flagged photos is an easy and
  invisible regression.

A lead, **not yet verified, do not ship it on the strength of this number**:
paper is smooth and that fabric is not, so gating the mask on local standard
deviation (`sd < 18` over a window of about 1.2% of the short side) takes mean
component coverage from 81% to 49% and takes the over-70% cases from 23 to 0.
That is only evidence that the blob stopped being the whole frame. It is NOT
evidence that the blob is now the paper. Look at the masks before believing
it, and check it against the receipt set too, which the current mask handles
correctly.

**Open question for the client, and it changes the target:** should a crop
cover only the top handwritten slip, or the whole stack including the printed
receipts under it? Detection currently aims at one sheet. If the answer is the
whole stack, the audit's premise becomes right again and detection is what
needs to change.

## Rotating: turn the PHOTO, not the result

The first version turned only the crop. The client spotted what that does:
"we have left original, right the result, now only result wala rotates". The
two panes end up at different angles, which is the one thing a side-by-side
must not do, since comparing them is its entire purpose.

So `R` now turns **the photo**, and everything the page holds in image
coordinates turns with it: the corners (`[h - y, x]`, ninety degrees
clockwise), the photo's width and height, the picture in the left pane, and
the edge map the magnet reads. `/api/photo`, `/api/edges`, `/api/snap`,
`/api/preview` and `/api/crop` all take `rotate` and apply it to the SOURCE
before anything else, so there is exactly one frame and no pointer coordinate
ever needs un-rotating. That last part is why this design was chosen over a
CSS transform on the pane: a rotated container makes every `getBoundingClientRect`
a transform waiting to be got wrong, and this project has already lost an
afternoon to one overlay coordinate bug.

**The invariant that proves it, and the test that enforces it:** turning the
photo then cropping must give the same picture as cropping then turning. It is
checked at 90, 180, 270 and 360 degrees in `cropui_selftest.py`, against a
reference built with `cv2.rotate`.

That test earned its keep immediately. It failed at first with a mean pixel
difference of 32, and the cause was `_PREVIEW_SRC` being keyed on the photo's
name alone: a request for the turned photo was served the cached unturned
copy, so the corners landed somewhere else entirely. `_LAST` had the same hole
and had just been fixed; the same mistake, made twice, in two caches. **Both
keys now carry everything the picture depends on.**

One test here deliberately does NOT compare pictures. "Four turns come back to
where it started" compares the corner numbers, because the previews are JPEG
encoded separately each time and comparing them would be measuring the
encoder, not the arithmetic. Four applications of the mapping is the identity,
exactly, and that is what is asserted.

`turn` resets to 0 for every photo: a photo opens the way it was taken.



`R` in the editor, a button beside it, and a "Turn finished crops upright"
checkbox that applies to the automatic crops in a batch run.

The gotcha: **rotate the result, not the corners.** `warp()` calls
`order_corners()` before it builds the transform, so re-ordering the quad to
rotate would be silently normalised away and nothing would happen.
`parchi.turn()` acts on the warped image, where a quarter turn is a transpose
and a flip, so it costs nothing and resamples nothing. `/api/preview` and
`/api/crop` both take `rotate`, so the After pane shows the turn as the
operator makes it and the saved file matches what they were looking at.

## A CONSOLE THAT VANISHES TAKES THE REASON WITH IT

Reported from the 1.2 release as "the main console is getting closed".

`cropui.py` ended with `sys.exit(main())`. Start Crop when it is already
running and `main()` printed two genuinely useful lines and returned 1. But
Crop is launched by double-clicking a .bat, and a console window closes the
instant the program ends, so the operator saw a black window blink and had
nothing to report but "it does not work". Every crash was the same: traceback
printed, window gone.

`parchi.py` has had `pause()` since the first version. `cropui.py` never got
it. Nobody noticed because a developer starts it from a terminal that stays
open by itself.

Now `hold_window()` waits for a keypress, and the top level catches
`KeyboardInterrupt` (somebody stopping it on purpose, code 0),
`SystemExit` (how uvicorn leaves when it cannot bind at all) and anything
else, printing the traceback and saying a photo of the window is enough to
report it.

Two things that must stay true:

It holds ONLY on failure, `if code:`. A normal stop should close cleanly, and
`/api/shutdown` calls `os._exit(0)` so it never reaches this code at all.

It must never hang when there is no keyboard. `sys.stdin.isatty()` guards it,
and the suite proves it: the test occupies the port, starts `cropui.py` with
`stdin=DEVNULL`, and fails if it is still alive after 60 seconds. That is not
hypothetical: `build.bat` runs these suites with no console, and a hang there
would block every future build.

The port-in-use message was rewritten at the same time. "Crop may already be
running. Run STOP_Crop.bat first." is a guess followed by an instruction. It
now says to look for the Crop-UI window or open the address, and to run
STOP_Crop.bat only if neither is there.

### One window, not two

`START_Crop.bat` used to launch the exe with `START`, which opens a SECOND
console. So the operator got two black windows: the launcher, saying "this
window can be closed", and the server, saying "leave this window open". They
contradict each other, the launcher then closed itself after eight seconds,
and that is what came back as the fault report "the main console is getting
closed". Nothing was wrong. The design was.

The exe now runs in the launcher's own window. One window, it is the server,
and closing it stops Crop, which is what closing it looks like it should do.
The startup message says so.

`STOP_Crop.bat` now tries three things, because each one can miss: `/IM
crop-ui.exe` is reliable for a shipped client, the window-title filter catches
a copy started from source where there is no exe to name, and the port scan
catches anything still holding 8112 after both, which is the thing that
actually stops the next start from working.

Neither .bat can be tested from a Linux container. They are short and plain on
purpose. Check both once on Windows after any change to them.

## MAKING THE FILES SMALLER, WHICH THE TOOL DID NOT DO AT ALL

The client said size cutting was the main purpose. Measured on the 26 real
parchis, before any of this:

```
originals from the phone     61.5 MB
what _parchi contained       60.3 MB
```

Two percent. Twenty-five of twenty-six photos were `shutil.copy2` byte for
byte, and `/api/keep` copied too, so the folder an operator finished was the
same size as the folder they started with.

### Cropping is not where the bytes are

On this client's photos the parchi fills the frame, so there is almost no
background to remove. The levers that work are resolution and JPEG quality:

```
originals                    61.5 MB   100%
full size at q92 (old)       47.0 MB    76%
2400px q90                   29.6 MB    48%
2000px q90                   21.4 MB    35%   <- the default
2000px q85                   16.3 MB    27%
1600px q85                   10.5 MB    17%
```

2000px at q90 was chosen by looking at the handwriting at 1:1, not by picking
a number. The digits and the Devanagari are intact; the photos are 2592x1936
so it is a modest step down, and `INTER_AREA` averages the sensor noise away,
which is why a shrunk parchi often looks *cleaner* at 1:1 than the original.
Do not raise the default without doing that comparison again.

Both levers cost about the same in megabytes, but they are not equivalent:
JPEG artefacts sit on high-contrast edges, which is exactly what a pen stroke
on white paper is. When something has to give, give resolution, not quality.
That is why 1600 is the only preset that also drops to q85.

### Where it happens

`fit(bgr, longest)` never enlarges and returns the same object when there is
nothing to do. `save()` takes a quality. Every file the tool writes goes
through one of two functions:

* `put_image(bgr, path, opts)` for a picture we made
* `put_original(src, path, opts, bgr=None)` for a photo passing through

`put_original` is the important one. With no size setting it is the byte copy
it always was, and the "untouched original" promise holds. With one, the copy
is resized, because that is where most photos go: 25 of 26. Shrinking only the
cropped ones would have saved 2% and looked like a feature.

NOT in `finish()`. `finish()` is also called by `/api/preview`, which works on
an already-scaled copy, so a resize there would be applied twice.

### The second door

`--smaller` on the command line, and "Just make the photos smaller" on the
folder card. No detection, no queue, no review: a folder, a button, and one
sentence at the end. For someone whose whole job is the size, the crop flow is
a detour past a queue they do not need.

Output goes to `_smaller` beside the photos, never over them, and the screen
says so before the button is pressed. `collect()` skips `_smaller` exactly as
it skips `_parchi`, so a second pass reads the originals rather than shrinking
the shrunk. A photo already inside the cap is copied byte for byte, not
re-encoded: re-encoding loses a little for no saving, and across passes that
loss would compound silently.

### The escape hatch

"Save this one at full size" in the editor strip, shown only when the run is
shrinking, rebuilt with every photo so it cannot leak into the rest of the
batch. It exists for a parchi with very small or very faint writing. It is
deliberately a tick and not a second size control: the size decision belongs
to the run, or the archive comes out as a mix decided by which photos happened
to need corner work.

### Measured after

```
--smaller over the 26     61.5 MB -> 21.4 MB   65% smaller
a normal crop run         61.5 MB -> 21.3 MB   65% smaller
```

### A trap in the tests

The synthetic photos are 1600x1200, under the 2000px default, so `fit()`
returns them untouched and a test at the default proves nothing. Both suites
use a cap of 700 or 800 so the resize actually bites. If you change the test
photos, check that cap still bites.

## THE CONTROLS GO UNDER THE PHOTOS, NOT BESIDE THEM

The editor was a three-column grid: photo, result, and a 236px column of
buttons down the right. That column cost the photos 236px of width for its
whole height, and then stood empty below the sixth button. The operator is
looking at the photos, not at the buttons, so that is the wrong way round.

Tasveer has the shape that works: both panes across the full width, controls
in a strip underneath, as tall as its contents and no taller.

```
.stage{grid-template-columns:1fr 1fr;
       grid-template-areas:"before after" "bar bar"}
```

`.side` keeps its name in the markup and becomes that strip: a wrapping flex
row holding the filename block, the two sliders, the magnet tick, and an
actions row.

Measured, photo width:

```
                 before   after
1366 x 768         532     627
1600 x 900         692     744
1920 x 1080        717     842
```

### The width rule, which replaced two breakpoints

The page is capped at 1180 for reading. The two panes are not read, they are
compared, so `#step-review` spills past that cap. It used to do so at two
hardcoded breakpoints, `-160px` at 1500 and `-310px` at 1820, which were right
at exactly those two widths and left about 250px unused at 1366.

`main` is 1180 wide with 26 of padding, so its content starts at
`(100vw - 1180)/2 + 26`. Pulling back by that, less a 24px gutter, is
`calc(590px - 50vw - 2px)`:

```css
@media (min-width:1260px){
  #step-review{margin-left:max(-310px, calc(590px - 50vw - 2px));
               margin-right:max(-310px, calc(590px - 50vw - 2px))} }
```

`max()` stops at -310px. Past about 1750 the panes are bigger than any parchi
needs and the operator is only moving their head further.

### Two things worth keeping

`.actions` is `flex-basis:100%`, so the buttons are always on their own line.
Letting them share the first line put them at a different place on every
window width, and a button that moves is a button you have to look for.

`say()` appends its message to `.side`, not to `.meta`. Inside the filename
block a sentence wrapped into a 150px column and pushed every button down as
it appeared.

Labels were shortened to fit a horizontal row: "Square off, trim 31%
background" became "Trim 31% background", "Bigger photo, result below" became
"Stack the panes". The `title` still carries the full explanation.

## THE SLIDERS ACT IN THE BROWSER, AND WHAT THAT UNCOVERED

The editor's brightness and contrast used to call `/api/preview` on every
slider step. Measured on a 2592x1936 photo the round trip is 33 ms, which is
fine, but it had to be debounced at 180 ms or a drag fired fifty warps and
fifty JPEG encodes for pictures thrown away in the next frame. So the visible
lag was about 215 ms: slow enough that a person stops trusting the slider and
starts guessing from the number instead, which is the thing the pane exists to
prevent.

Brightness and contrast are one multiply and one add per channel. The browser
already has the picture. So the page does it:

```html
<filter id="adj" color-interpolation-filters="sRGB">
  <feComponentTransfer>
    <feFuncR type="linear" slope="<contrast>" intercept="<offset/255>"/>
```

`type="linear"` is `out = slope*in + intercept`, clamped, per channel. That is
`parchi.adjust()` exactly. `applyLook()` sets the two numbers and points the
preview image at the filter; moving the slider does no fetch at all.

Measured after: 16 slider steps in 34 ms with **zero** network requests, and
the saved file came out 0.08 levels away from what was on the screen.

`color-interpolation-filters="sRGB"` is not decoration. The SVG default is
linearRGB, which would do the arithmetic on different numbers and show a
picture the saved file does not match.

`/api/preview` is now asked for a plain crop (`brightness: 0, contrast: 1`).
The batch "lift contrast for faint pen" still happens on the server, because
CLAHE is adaptive and cannot be done this way, and the browser's linear step
goes on top: the same order `finish()` uses.

### The bug the browser found

Checking the filter against OpenCV, seven cases matched and three did not, all
at the dark end. The browser was right.

`cv2.convertScaleAbs` takes the **absolute value** before clamping. So
darkening, or raising contrast, sent black back up towards white:

```
contrast 1.5, input   0  ->  convertScaleAbs 51,  correct 0
brightness -40, input 0  ->  convertScaleAbs 40,  correct 0
```

At contrast 1.5 every level under 43 came out inverted. That is ink and
shadow on a bill: 5.1% of the pixels on a real parchi, up to 64 levels out.
Nobody had noticed because a parchi crop is mostly bright paper and the
inversion hides in the dark strokes.

`adjust()` now builds a lookup table with `np.clip`, which is what every other
program does and what a person expects.

Tests: `parchi_selftest.py`, "[brightness and contrast clip, they do not
wrap]" checks the properties rather than the arithmetic, so black cannot go
up and mid grey cannot move. `cropui_selftest.py`, "[the sliders act in the
browser, and must not drift]" reads the slope and intercept formula out of the
served page and compares what the browser would paint against what
`parchi.adjust()` saves, across the whole slider range. If the two ever drift,
the pane says one thing and the file is another, which is the exact lie this
page exists not to tell.

## A SECOND RUN USED TO DESTROY A DAY OF SOMEBODY'S WORK

This was the worst bug in the tool, and nothing on screen said it was
happening. The reproduction, on one real parchi:

```
automatic crop                     852,453 bytes
after the operator corrects it     244,852 bytes
after running the folder again     852,453 bytes   <- their correction, gone
```

The run loop cropped every photo it found, and the operator's file was simply
in the way. Worse, a photo the operator had finished by hand was also written
into `2_review`, so the folder then claimed the work still needed doing. An
operator who ran the folder twice — which is the obvious thing to do when more
photos arrive in the same folder — lost an afternoon and had no way to know.

Everything else in this tool is about not destroying pixels. This destroyed
human effort, which is worth more.

### The fix: write the decision down

`_parchi/decided.csv`, two columns, `file,bucket`. A person's decision is
written there the moment they make it:

* `/api/crop` and `/api/keep` call `parchi.mark_decided()`.
* `/api/undo` calls `parchi.forget_decided()`. Undo means undo — a photo taken
  back out of the record may be cropped again by a later run.

Both `parchi.main()` and `cropui.run_job()` read it before they start and skip
every photo named in it, reporting "N photos were already finished by hand and
were left alone". Skipping also skips `detect()`, which is the expensive part,
so a second run over a mostly-finished folder is fast.

It is a plain CSV on purpose. Anyone can open it, read it, and delete it. The
record is not a database and must never become one.

### Why the record, and not a timestamp comparison

Comparing mtimes was the first idea and it is wrong twice over: copying a
folder loses the relationship, and a run that legitimately should re-crop looks
identical to one that should not. The record stores what a *person decided*,
which is the thing we actually care about, and nothing else infers it.

### Start fresh

`--redo` on the command line, a "Start fresh" tick box on the Settings card.
It deletes the record and crops everything again. It **must** delete the
record rather than merely ignore it: leaving it behind would have it claim
photos were finished by hand when an automatic crop had just replaced them,
which is a lie stored on disk.

### `_previous/`, and the one action with no way back

A photo settled in an *earlier* session has no outline recorded, so undo cannot
rebuild it. Cropping it again would therefore be the only action in the page
that destroys something unrecoverably — and what it destroys is the exact thing
all of the above exists to protect. So `remember_earlier_work()` moves the old
file to `_parchi/_previous/<name>` before the new crop is written, and undo
moves it back, byte for byte. Keep the file, not the recipe: a recipe re-run
with different settings does not give the same picture.

`_previous` is inside `_parchi`, so `parchi.collect()` skips it on later runs
and `clear_from_buckets()` (which walks `parchi.BUCKETS` only) never touches it.

### What is checked, and where

`parchi_selftest.py`, "safety and input handling": a second run leaves a
hand-finished file alone, says so, does not also file it for review, and
`--redo` crops it again and clears the record.

`cropui_selftest.py`, "[a second run keeps what a person decided]": the full
round trip through the server — files compared byte for byte, `leftAlone`
reported, settled photos still measured so the editor can open on them, undo
restoring the earlier session's own file, and start fresh clearing the record.

Do not delete these. This is the bug that was invisible for a whole release.

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
