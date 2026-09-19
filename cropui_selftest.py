"""
cropui_selftest.py : start the review server, drive it, check what it does.

    python cropui_selftest.py

Runs on a spare port so it will not disturb a Crop window you already have
open.  Uses the same generated photos as parchi_selftest.py, so no real parchi
photos are needed.
"""

import filecmp
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import subprocess

import cv2
import numpy as np
import uvicorn

import cropui
import parchi
import parchi_selftest as st

PORT = 8113
BASE = "http://127.0.0.1:{}".format(PORT)
passed = failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  ok    {}".format(name))
    else:
        failed += 1
        print("  FAIL  {}  {}".format(name, str(detail)[:200]))


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def main():
    work = Path(tempfile.mkdtemp(prefix="cropui-selftest-"))
    photos = work / "photos"
    try:
        truth = st.build_set(photos)

        config = uvicorn.Config(cropui.app, host="127.0.0.1", port=PORT, log_level="error")
        threading.Thread(target=uvicorn.Server(config).run, daemon=True).start()
        for _ in range(40):
            try:
                get("/")
                break
            except Exception:
                time.sleep(0.25)

        print("[page and folder picker]")
        check("page loads", get("/")[0] == 200)
        status, body = get("/api/browse?path=" + str(photos))
        check("folder browser counts the photos",
              status == 200 and json.loads(body)["images"] == len(truth), body[:120])
        status, _ = get("/api/browse?path=" + str(photos / "nowhere"))
        check("missing folder refused", status == 404)

        print("\n[drop, paste and browse]")
        status, body = post("/api/resolve", {"text": str(photos)})
        check("a pasted path is accepted",
              status == 200 and json.loads(body)["path"] == str(photos), body[:150])
        check("and it counts the photos",
              json.loads(body)["images"] == len(truth), body[:150])

        as_uri = Path(photos).as_uri()
        status, body = post("/api/resolve", {"text": as_uri})
        check("a file:// drop from Explorer is accepted",
              status == 200 and json.loads(body)["path"] == str(photos), body[:150])

        one_photo = sorted(photos.glob("*.jpg"))[0]
        status, body = post("/api/resolve", {"text": one_photo.as_uri()})
        check("dropping a photo uses the folder it is in",
              status == 200 and json.loads(body)["path"] == str(photos), body[:150])

        status, body = post("/api/resolve", {"text": as_uri + "\n" + one_photo.as_uri()})
        check("a multi-line drop uses the first entry",
              status == 200 and json.loads(body)["path"] == str(photos), body[:150])

        status, _ = post("/api/resolve", {"text": str(photos / "not-real")})
        check("a folder that is not there is refused", status == 404)
        status, _ = post("/api/resolve", {"text": "   "})
        check("an empty drop is refused", status == 400)

        if os.name != "nt":
            status, _ = post("/api/pick", {})
            check("native picker reports it is Windows only", status == 501, status)
        else:
            print("  skip  native picker (it would open a dialog and wait)")

        print("\n[running detection]")
        status, body = post("/api/run", {"folder": str(photos), "confidence": 62,
                                         "review": 35, "margin": 1})
        check("run starts", status == 200, body)
        state = {}
        for _ in range(240):
            state = json.loads(get("/api/progress")[1])
            if not state["running"] and state["total"] and state["done"] == state["total"]:
                break
            time.sleep(0.5)
        check("run finishes every photo", state.get("done") == len(truth), state)

        items = json.loads(get("/api/results")[1])["items"]
        check("a result per photo", len(items) == len(truth), len(items))
        check("buckets are the ones parchi uses",
              {i["bucket"] for i in items} <= set(parchi_buckets()), 
              {i["bucket"] for i in items})
        check("page is told the detected corners",
              all(i["quad"] is None or len(i["quad"]) == 4 for i in items))

        print("\n[images]")
        name = items[0]["name"]
        status, body = get("/api/photo?kind=thumb&name=" + name)
        check("thumbnail served", status == 200 and body[:2] == b"\xff\xd8")
        status, body = get("/api/photo?kind=full&name=" + name)
        check("full photo served", status == 200 and body[:2] == b"\xff\xd8")
        status, _ = get("/api/photo?kind=full&name=" + "no_such_file.jpg")
        check("unknown photo refused", status == 404)

        print("\n[fixing by hand]")
        queue = [i for i in items if i["bucket"] != "1_cropped"]
        target = (queue or items)[0]
        w, h = target["width"], target["height"]
        status, body = post("/api/crop", {
            "name": target["name"],
            "quad": [w * .1, h * .1, w * .9, h * .1, w * .9, h * .9, w * .1, h * .9]})
        check("hand crop accepted", status == 200, body)

        out = photos / "_parchi" / "1_cropped" / target["name"]
        check("hand crop written to 1_cropped", out.exists())
        image = cv2.imread(str(out))
        check("hand crop is the size that was dragged",
              image is not None and abs(image.shape[1] - w * .8) < w * .06,
              None if image is None else image.shape)
        check("no copy left behind in the old bucket",
              not (photos / "_parchi" / "2_review" / target["name"]).exists()
              and not (photos / "_parchi" / "3_not_detected" / target["name"]).exists())
        after = json.loads(get("/api/results")[1])["items"]
        row = [i for i in after if i["name"] == target["name"]][0]
        check("marked as fixed by hand", row["bucket"] == "1_cropped" and row["edited"], row)

        other = [i for i in after if i["name"] != target["name"]][0]
        status, body = post("/api/keep", {"name": other["name"]})
        check("keep whole photo accepted", status == 200, body)
        kept = photos / "_parchi" / "1_cropped" / other["name"]
        check("kept photo is the untouched original",
              kept.exists() and kept.stat().st_size == (photos / other["name"]).stat().st_size)

        print("\n[magnet]")
        import numpy as np
        # A photo with edges that can actually be found: whether the magnet
        # refuses to move on a hopeless one is the next check's business.
        sample = next(i for i in after if i["quad"] and i["name"].startswith("easy_"))
        true_quad = np.array(truth[sample["name"]], dtype="float64")
        rough = true_quad + np.array([[18, -22], [-25, 16], [20, 19], [-16, -20]],
                                     dtype="float64")
        status, body = post("/api/snap", {"name": sample["name"],
                                          "quad": rough.flatten().tolist()})
        check("snap accepted", status == 200, body)
        snapped = np.array(json.loads(body)["quad"], dtype="float64")

        import parchi
        def spread(q):
            return float(np.mean(np.linalg.norm(
                parchi.order_corners(q) - parchi.order_corners(true_quad), axis=1)))
        check("snap moves the outline closer to the real edge",
              spread(snapped) < spread(rough),
              "{:.1f}px before, {:.1f}px after".format(spread(rough), spread(snapped)))
        check("snap does not fling the outline somewhere else",
              float(np.max(np.linalg.norm(
                  parchi.order_corners(snapped) - parchi.order_corners(rough), axis=1))) < 120,
              "moved too far")

        status, _ = post("/api/snap", {"name": "not_a_photo.jpg",
                                       "quad": [0, 0, 9, 0, 9, 9, 0, 9]})
        check("snap on an unknown photo refused", status == 404)

        print("\n[edge map for the live magnet]")
        status, body = get("/api/edges?name=" + sample["name"])
        check("edge map served", status == 200 and body[:8] == b"\x89PNG\r\n\x1a\n", status)
        arr = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        check("it is a small greyscale picture",
              arr is not None and max(arr.shape) <= 720, None if arr is None else arr.shape)
        check("it is small enough to fetch per photo",
              len(body) < 400_000, "{} bytes".format(len(body)))
        check("it actually marks edges, not a blank image",
              arr is not None and arr.max() > 200 and arr.mean() < 120,
              None if arr is None else (int(arr.max()), round(float(arr.mean()))))
        status, _ = get("/api/edges?name=not_a_photo.jpg")
        check("edge map on an unknown photo refused", status == 404)

        print("\n[side by side preview]")
        status, body = post("/api/preview", {"name": sample["name"],
                                             "quad": true_quad.flatten().tolist()})
        check("preview returns an image", status == 200 and body[:2] == b"\xff\xd8", status)
        shown = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
        check("preview decodes", shown is not None)

        # "Is it the slip or the whole photo?" cannot be settled by how big the
        # picture is (the pane decides that) nor by its shape (this slip is very
        # nearly the shape of its photo).  Ask for a different quad instead: a
        # server ignoring the corners would hand back the same picture twice.
        sw, sh = sample["width"], sample["height"]
        _, body_whole = post("/api/preview",
                             {"name": sample["name"],
                              "quad": [0, 0, sw - 1, 0, sw - 1, sh - 1, 0, sh - 1]})
        whole = cv2.imdecode(np.frombuffer(body_whole, dtype=np.uint8), cv2.IMREAD_COLOR)
        gap = None
        if shown is not None and whole is not None:
            a = cv2.resize(shown, (96, 96), interpolation=cv2.INTER_AREA)
            b = cv2.resize(whole, (96, 96), interpolation=cv2.INTER_AREA)
            gap = float(np.abs(a.astype("float32") - b.astype("float32")).mean())
        check("preview follows the corners rather than showing the whole photo",
              gap is not None and gap > 2.0, gap)
        status, _ = post("/api/preview", {"name": sample["name"],
                                          "quad": [0, 0, 0, 0, 0, 0, 0, 0]})
        check("preview refuses corners that make no crop", status == 400, status)
        status, _ = post("/api/crop", {"name": sample["name"],
                                       "quad": [0, 0, 0, 0, 0, 0, 0, 0]})
        check("cropping to nothing is refused too", status == 400, status)
        status, _ = post("/api/preview", {"name": "nope.jpg", "quad": [0,0,9,0,9,9,0,9]})
        check("preview on an unknown photo refused", status == 404)

        # The preview pane sits beside the photo at the same size, so it has to
        # be able to come back big; and it warps a scaled-down copy to stay
        # quick, so it also has to come back the same *shape* as the real crop.
        def preview_shape(quad, size=None):
            payload = {"name": sample["name"], "quad": list(quad)}
            if size is not None:
                payload["size"] = size
            st_, bd = post("/api/preview", payload)
            if st_ != 200:
                return None
            return cv2.imdecode(np.frombuffer(bd, dtype=np.uint8),
                                cv2.IMREAD_COLOR).shape[:2]

        flat = true_quad.flatten().tolist()
        small = preview_shape(flat, 400)
        big = preview_shape(flat, 1200)
        check("a bigger pane gets a bigger preview",
              small and big and max(big) > max(small), (small, big))
        check("the preview never exceeds the size asked for",
              small and big and max(small) <= 400 and max(big) <= 1200, (small, big))
        # asking small first and big second must not serve the small copy again
        check("asking again for a larger preview really enlarges it",
              preview_shape(flat, 1200) == big, (big, preview_shape(flat, 1200)))

        status, _ = post("/api/crop", {"name": sample["name"], "quad": flat})
        check("the same corners crop for real", status == 200, status)
        saved = cv2.imread(str(photos / "_parchi" / "1_cropped" / sample["name"]))
        shape_big = preview_shape(flat, 1200)
        if saved is not None and shape_big:
            want = saved.shape[0] / float(saved.shape[1])
            got = shape_big[0] / float(shape_big[1])
            check("the preview is the same shape as the crop that gets saved",
                  abs(want - got) / want < 0.02, (saved.shape[:2], shape_big))
        else:
            check("the preview is the same shape as the crop that gets saved",
                  False, "no saved crop to compare against")

        print("\n[rotation]")
        def shape_at(rot):
            st_, bd = post("/api/preview", {"name": sample["name"], "quad": flat,
                                            "size": 700, "rotate": rot})
            if st_ != 200:
                return None
            return cv2.imdecode(np.frombuffer(bd, dtype=np.uint8),
                                cv2.IMREAD_COLOR).shape[:2]
        # Rotation turns the PHOTO, so the corners have to be carried into the
        # turned frame with it, exactly as the page does.  Sending the same
        # corners against a turned photo describes a different region, which
        # is why these used to compare shapes and now compare pictures.
        def turn_quad(flat_quad, width, height):
            pts = [[flat_quad[i], flat_quad[i + 1]] for i in range(0, 8, 2)]
            return [c for p in [[height - p[1], p[0]] for p in pts] for c in p]

        def picture_at(quad, rot):
            st_, bd = post("/api/preview", {"name": sample["name"], "quad": quad,
                                            "size": 500, "rotate": rot})
            if st_ != 200:
                return None
            return cv2.imdecode(np.frombuffer(bd, dtype=np.uint8), cv2.IMREAD_COLOR)

        def unlike(a, b):
            if a is None or b is None:
                return 999.0
            small = lambda m: cv2.resize(m, (160, 160)).astype("float32")
            return float(np.abs(small(a) - small(b)).mean())

        start = picture_at(flat, 0)
        check("a preview comes back for the unturned photo", start is not None)
        carried, wide, high = list(flat), sample["width"], sample["height"]
        for step in (1, 2, 3, 4):
            carried = turn_quad(carried, wide, high)
            wide, high = high, wide
            got = picture_at(carried, (step * 90) % 360)
            want = start
            for _ in range(step):
                want = cv2.rotate(want, cv2.ROTATE_90_CLOCKWISE)
            # turning the photo then cropping must equal cropping then turning
            check("turn {} degrees: the crop is the same picture, turned"
                  .format((step * 90) % 360), unlike(got, want) < 6.0,
                  round(unlike(got, want), 2))
        # The pictures are JPEG-encoded separately each time, so comparing them
        # here would be measuring the encoder.  What must be exact is the
        # arithmetic: four applications of the corner mapping is the identity.
        check("four turns bring the corners back exactly",
              [round(c, 3) for c in carried] == [round(c, 3) for c in flat],
              (flat, carried))
        check("and the photo is back to its own size",
              (wide, high) == (sample["width"], sample["height"]))
        check("nonsense rotation is refused or ignored, never a crash",
              picture_at(flat, 45) is not None)

        rot_name = sample["name"]
        before = cv2.imread(str(photos / "_parchi" / "1_cropped" / rot_name))
        turned = turn_quad(list(flat), sample["width"], sample["height"])
        status, _ = post("/api/crop", {"name": rot_name, "quad": turned, "rotate": 90})
        after = cv2.imread(str(photos / "_parchi" / "1_cropped" / rot_name))
        check("a crop taken on a turned photo is saved turned",
              status == 200 and before is not None and after is not None
              and before.shape[:2] == after.shape[:2][::-1],
              (None if before is None else before.shape[:2],
               None if after is None else after.shape[:2]))

        page = get("/")[1].decode("utf-8")
        # the shortcut hints live inside the buttons; rotateResult relabels
        # that button, and textContent there would throw the hint away
        for btn, key in [("btnCrop","C"),("btnSnap","M"),("btnTurn","R"),
                         ("btnKeep","K"),("btnSkip","S")]:
            seg = page.split('id="%s"' % btn, 1)
            ok = len(seg) > 1 and "<kbd>%s</kbd>" % key in seg[1][:160]
            check("the %s button carries <kbd>%s</kbd>" % (btn, key), ok)
        check("rotating relabels with innerHTML so the hint survives",
              'btn.innerHTML = (turn ?' in page)

        for piece, why in [('id="btnTurn"', "the rotate button"),
                           ('id="upr"', "the upright setting"),
                           ("rotate: turn", "the editor sending its rotation"),
                           ("quad.map(p => [h - p[1], p[0]])",
                            "the editor carrying the corners into the turned frame"),
                           ('"&rotate=" + turn', "the photo reloading turned"),
]:
            check("the page has {}".format(why), piece in page)
        check("the preview cache is keyed on the turn",
              '_PREVIEW_SRC["key"]' in open(str(Path(cropui.__file__))).read())

        print("\n[clearing several at once]")
        page = get("/")[1].decode("utf-8")
        for piece, why in [
                ('id="pickRow"', "the thumbnail picker"),
                ('id="pickKeep"', "the keep-these button"),
                ('id="pickFlagged"', "tick the flagged ones"),
                ('id="pickAll"', "tick all"),
                ('id="pickNone"', "untick all"),
                ("async function keepPicked", "keeping the ticked ones"),
                ("queue.filter(x => !x.edited)", "only saved photos leaving the queue")]:
            check("the page has {}".format(why), piece in page)
        # every photo still awaiting a decision must be offered, not just the
        # ones the tool put a note on: on the real parchis the notes covered
        # 17 of 25, and a person could clear all 25 from the thumbnails.
        check("the picker is built from the whole queue, not just flagged ones",
              "const pending = queue.slice(qi);" in page)
        # The button ticks every flagged photo, so it must not promise to tick
        # only close-ups.  On the 26 real parchis the flags were 10 close-ups
        # and 7 audit vetoes, and calling all 17 close-ups was simply untrue.
        check("the flag button counts what it will actually tick",
              '>Tick the \' + flagged' in page)
        check("close-ups and audit vetoes are counted apart",
              "const closeUps = pending.filter(isCloseUp).length;" in page
              and "const vetoed = pending.filter(x => x.note && !isCloseUp(x))" in page)
        check("neither kind is described as the other",
              "of them look like close-ups" not in page)

        left = [i for i in json.loads(get("/api/results")[1])["items"]
                if i["bucket"] != "1_cropped"]
        if left:
            target = left[0]
            status, _ = post("/api/keep", {"name": target["name"]})
            check("a photo can be kept whole straight from the picker", status == 200)
            row = [i for i in json.loads(get("/api/results")[1])["items"]
                   if i["name"] == target["name"]][0]
            check("keeping it files it as done", row["bucket"] == "1_cropped", row)
            kept = photos / "_parchi" / "1_cropped" / target["name"]
            check("the kept photo is the original, byte for byte",
                  kept.exists() and filecmp.cmp(str(kept), target["source"], shallow=False))
        status, _ = post("/api/keep", {"name": "not_a_photo.jpg"})
        check("keeping an unknown photo refused", status == 404)

        print("\n[trimming the background off the sides that show it]")
        import parchi as _p
        shot = cv2.imread(str(photos / sample["name"]))
        h, w = shot.shape[:2]
        inner = np.array([[w*.3, h*.3], [w*.7, h*.3],
                          [w*.7, h*.7], [w*.3, h*.7]], dtype="float32")
        t = _p.trim_to_frame(inner, shot.shape)
        check("a trim is never smaller than the outline it came from",
              t is not None and t[:,0].min() <= inner[:,0].min()
              and t[:,1].min() <= inner[:,1].min()
              and t[:,0].max() >= inner[:,0].max()
              and t[:,1].max() >= inner[:,1].max(), (inner.tolist(), None if t is None else t.tolist()))
        check("an outline well inside the frame is trimmed to itself",
              t is not None and abs(t[:,0].min() - w*.3) < 2
              and abs(t[:,0].max() - w*.7) < 2)

        edgey = np.array([[3, 3], [w-3, 3], [w-3, h*.6], [3, h*.6]], dtype="float32")
        t2 = _p.trim_to_frame(edgey, shot.shape)
        check("a side already at the frame is pushed out TO the frame, not in",
              t2 is not None and t2[:,0].min() == 0 and t2[:,1].min() == 0
              and t2[:,0].max() == w, None if t2 is None else t2.tolist())
        check("a side with background left is not pushed out",
              t2 is not None and t2[:,1].max() < h, None if t2 is None else t2.tolist())

        whole = np.array([[0,0],[w,0],[w,h],[0,h]], dtype="float32")
        t3 = _p.trim_to_frame(whole, shot.shape)
        check("an outline that is already the frame trims nothing",
              t3 is not None and abs(float((t3[2][0]-t3[0][0])*(t3[2][1]-t3[0][1]))
                                     - w*h) < w)
        check("no outline means no trim", _p.trim_to_frame(None, shot.shape) is None)

        check("the frame-edge count reads the outline, not a paper mask",
              _p.quad_touches_frame(whole, shot.shape) == 4
              and _p.quad_touches_frame(inner, shot.shape) == 0)

        page = get("/")[1].decode("utf-8")
        check("results carry the trim and how much it removes",
              '"trim"' in open(str(Path(cropui.__file__))).read()
              and "trimTakes" in open(str(Path(cropui.__file__))).read())
        check("the editor never opens on the trim by itself",
              "usingTrim = false;" in page)
        check("the trim button is offered only when it removes something",
              "item.trimTakes >= 0.05" in page)
        check("the button says how much it will remove",
              'Math.round((item.trimTakes||0)*100) + "% background' in page)

        row = [i for i in json.loads(get("/api/results")[1])["items"]
               if i["name"] == sample["name"]][0]
        check("trimTakes survives JSON as a plain number",
              isinstance(row.get("trimTakes"), (int, float)), row.get("trimTakes"))

        print("\n[brightness and contrast]")
        import parchi as _pc
        flat_grey = np.full((40, 40, 3), 100, np.uint8)
        check("brightness shifts every pixel by exactly the slider value",
              int(_pc.adjust(flat_grey, 30, 1.0).mean()) == 130,
              int(_pc.adjust(flat_grey, 30, 1.0).mean()))
        check("zero brightness and unit contrast change nothing at all",
              np.array_equal(_pc.adjust(flat_grey, 0, 1.0), flat_grey))
        # contrast has to pivot on mid grey, or the two sliders fight
        mid = np.full((40, 40, 3), 128, np.uint8)
        check("contrast leaves mid grey where it is",
              int(_pc.adjust(mid, 0, 1.8).mean()) == 128,
              int(_pc.adjust(mid, 0, 1.8).mean()))
        check("contrast pushes a light tone further from mid grey",
              int(_pc.adjust(np.full((8, 8, 3), 160, np.uint8), 0, 1.5).mean()) == 176)
        check("contrast pulls a light tone towards mid grey when below one",
              int(_pc.adjust(np.full((8, 8, 3), 160, np.uint8), 0, 0.5).mean()) == 144)
        check("it clamps instead of wrapping round",
              int(_pc.adjust(np.full((8, 8, 3), 250, np.uint8), 80, 1.0).max()) == 255)

        # one place decides what a finished crop looks like
        src = cv2.imread(str(photos / sample["name"]))
        check("finish with nothing asked for returns the image untouched",
              np.array_equal(_pc.finish(src, {}), src))
        # asserting the mean moves by exactly the slider value only holds on an
        # image with headroom.  A crop of a bill is mostly bright paper, so it
        # clips at 255 and moves less.  Test the property that is always true.
        headroom = np.full((40, 40, 3), 90, np.uint8)
        check("finish applies the brightness it is given",
              abs(float(_pc.finish(headroom, {"brightness": 20}).mean()) - 110) < 1.0,
              float(_pc.finish(headroom, {"brightness": 20}).mean()))
        ladder = [float(_pc.finish(src, {"brightness": b}).mean())
                  for b in (-60, -20, 0, 20, 60)]
        check("on a real crop it still only ever goes one way",
              all(a < b for a, b in zip(ladder, ladder[1:])),
              [round(v, 1) for v in ladder])
        server = open(str(Path(cropui.__file__))).read()
        check("no endpoint reaches past finish() to the enhancer",
              "enhance_image" not in server)
        check("undo rebuilds with the run's settings, not the sliders",
              "parchi.finish(result, JOB[\"opts\"])" in server)

        # the pane says "what you will get", so it must include the lift
        _, plain = post("/api/preview", {"name": sample["name"], "quad": flat,
                                         "size": 400, "brightness": 0})
        _, lifted = post("/api/preview", {"name": sample["name"], "quad": flat,
                                          "size": 400, "brightness": 45})
        dec = lambda b: cv2.imdecode(np.frombuffer(b, dtype=np.uint8), cv2.IMREAD_COLOR)
        # again: a bright crop clips, so the size of the change is a property
        # of the photo.  What must hold is that it changed, and upwards.
        check("the preview actually shows the brightness asked for",
              float(dec(lifted).mean()) - float(dec(plain).mean()) > 5,
              round(float(dec(lifted).mean()) - float(dec(plain).mean()), 1))
        _, darker = post("/api/preview", {"name": sample["name"], "quad": flat,
                                          "size": 400, "brightness": -45})
        check("and downwards when asked to go down",
              float(dec(darker).mean()) < float(dec(plain).mean()) - 20,
              round(float(dec(plain).mean()) - float(dec(darker).mean()), 1))

        status, _ = post("/api/crop", {"name": sample["name"], "quad": flat,
                                       "brightness": 45})
        saved = cv2.imread(str(photos / "_parchi" / "1_cropped" / sample["name"]))
        check("and the saved file has it too",
              status == 200 and saved is not None
              and abs(float(saved.mean()) - float(dec(lifted).mean())) < 8,
              None if saved is None else round(float(saved.mean()) - float(dec(lifted).mean()), 1))

        page = get("/")[1].decode("utf-8")
        for piece, why in [('id="bri"', "the batch brightness slider"),
                           ('id="con"', "the batch contrast slider"),
                           ('id="eBri"', "the per photo brightness slider"),
                           ('id="eCon"', "the per photo contrast slider"),
                           ('id="btnPlain"', "reset both"),
                           ("look.brightness", "the editor sending its settings")]:
            check("the page has {}".format(why), piece in page)

        print("\n[the page itself]")
        page = get("/")[1].decode("utf-8")
        for piece, why in [
                ('id="preview"', "the After pane"),
                ('Original photo', "the Before label"),
                ('id="btnLayout"', "the side-by-side / wide toggle"),
                ('id="magnet" checked', "the magnet ticked in the markup"),
                ("let magnetOn = true;", "the magnet on at the start of a sitting"),
                ("magnet.checked = magnetOn;", "every photo opening with it on"),
                ('id="compare"', "the comparison overlay"),
                ('id="cmpA"', "the original half of the comparison"),
                ('id="cmpB"', "the cropped half of the comparison"),
                ('function openCompare', "opening a comparison"),
                ('async function undoCrop', "one undo for both places")]:
            check("the page still has {}".format(why), piece in page)

        check("the magnet setting is not remembered between sittings",
              "crop.magnet" not in page,
              "the page still stores it")

        print("\n[undo]")
        target2 = next(i for i in json.loads(get("/api/results")[1])["items"]
                       if i["bucket"] != "1_cropped")
        was = target2["bucket"]
        w2, h2 = target2["width"], target2["height"]
        status, _ = post("/api/crop", {"name": target2["name"],
                                       "quad": [w2*.2, h2*.2, w2*.8, h2*.2,
                                                w2*.8, h2*.8, w2*.2, h2*.8]})
        check("a photo can be cropped", status == 200)
        check("the crop is on disk",
              (photos / "_parchi" / "1_cropped" / target2["name"]).exists())

        status, body = post("/api/undo", {"name": target2["name"]})
        check("undo accepted", status == 200, body)
        check("undo puts it back where it started",
              json.loads(body)["bucket"] == was, body)
        check("the crop is gone from the finished folder",
              not (photos / "_parchi" / "1_cropped" / target2["name"]).exists())
        back = photos / "_parchi" / was / target2["name"]
        check("the original is back, byte for byte",
              back.exists() and filecmp.cmp(back, photos / target2["name"], shallow=False))
        rowback = [i for i in json.loads(get("/api/results")[1])["items"]
                   if i["name"] == target2["name"]][0]
        check("it is queued for checking again",
              rowback["bucket"] == was and not rowback["edited"], rowback)

        status, _ = post("/api/undo", {"name": "nope.jpg"})
        check("undo on an unknown photo refused", status == 404)

        print("\n[the sliders act in the browser, and must not drift]")
        # Brightness and contrast are applied by an SVG filter in the page as
        # the slider moves, so no server round trip and no debounce.  That is
        # only safe while the page's arithmetic matches parchi.adjust(): if
        # the two drift, the pane says one thing and the saved file is
        # another, which is the exact lie this page exists not to tell.
        import re
        page = get("/")[1].decode("utf-8")
        check("the page carries the adjustment filter",
              'id="adj"' in page and "feComponentTransfer" in page)
        check("the filter works in sRGB, not the linearRGB default",
              'color-interpolation-filters="sRGB"' in page)
        check("all three channels are transformed",
              all('feFunc{} type="linear"'.format(ch) in page for ch in "RGB"))
        formula = re.search(r"const slope = look\.contrast;\s*"
                            r"const intercept = \(128 \* \(1 - look\.contrast\)"
                            r" \+ look\.brightness\) / 255;", page)
        check("the page uses the same slope and intercept as parchi.adjust()",
              formula is not None)
        if formula:
            ramp = np.arange(256, dtype=np.uint8).reshape(1, 256, 1).repeat(3, axis=2)
            worst = 0
            for bri, con in [(0, 1.0), (40, 1.0), (-40, 1.0), (0, 1.5),
                             (0, 0.7), (25, 1.3), (-20, 0.8), (80, 2.0)]:
                slope = con
                intercept = (128 * (1 - con) + bri) / 255.0
                # what the browser paints: clamp(slope*x + intercept), in 0..1
                browser = np.clip(np.arange(256) / 255.0 * slope + intercept, 0, 1)
                browser = np.round(browser * 255).astype(int)
                server = parchi.adjust(ramp, bri, con)[0, :, 0].astype(int)
                worst = max(worst, int(np.abs(browser - server).max()))
            check("what the browser paints matches what the server saves",
                  worst <= 1, "{} levels apart".format(worst))
        check("the page asks for a plain preview and adjusts it itself",
              "brightness: 0, contrast: 1" in page)
        check("moving a slider does not fetch a preview",
              "applyLook();                 // no fetch" in page)

        print("\n[a second run keeps what a person decided]")
        # Running the same folder twice used to crop every photo again and
        # write the machine's answer over the operator's.  The file changed on
        # disk, the page said "cropped", and nothing anywhere said their work
        # had gone.  This is the check that stops that coming back.
        outroot = photos / "_parchi"
        record = parchi.load_decided(outroot)
        check("the hand decisions were written down", len(record) >= 2, record)
        settled_files = {n: (outroot / b / n).read_bytes()
                         for n, b in record.items() if (outroot / b / n).exists()}
        check("their finished files are on disk", len(settled_files) == len(record),
              (len(settled_files), len(record)))

        def run_again(**extra):
            body = {"folder": str(photos), "confidence": 62, "review": 35, "margin": 1}
            body.update(extra)
            status, body_out = post("/api/run", body)
            check("a second run starts", status == 200, body_out)
            state = {}
            for _ in range(240):
                state = json.loads(get("/api/progress")[1])
                if not state["running"] and state["total"]:
                    break
                time.sleep(0.5)
            return state

        state = run_again()
        check("it says how many it left alone",
              state.get("leftAlone") == len(record), state.get("leftAlone"))
        kept_same = [n for n, blob in settled_files.items()
                     if (outroot / record[n] / n).exists()
                     and (outroot / record[n] / n).read_bytes() == blob]
        check("every hand-finished file survived byte for byte",
              len(kept_same) == len(settled_files),
              sorted(set(settled_files) - set(kept_same)))
        check("none of them was also filed for review again",
              not any((outroot / "2_review" / n).exists() for n in record))
        rows = {i["name"]: i for i in json.loads(get("/api/results")[1])["items"]}
        check("the page marks them as settled",
              all(rows[n].get("settled") for n in record),
              [n for n in record if not rows.get(n, {}).get("settled")])
        check("a settled photo is still measured, so it can be opened",
              all(rows[n]["width"] > 0 and rows[n]["height"] > 0 for n in record),
              [(n, rows[n]["width"], rows[n]["height"]) for n in record])
        check("photos nobody decided were processed as usual",
              any(not i.get("settled") for i in rows.values()))

        # Touching one again must leave a way back to what they had.
        again = sorted(record)[0]
        rw, rh = rows[again]["width"], rows[again]["height"]
        status, body = post("/api/crop", {
            "name": again,
            "quad": [rw * .2, rh * .2, rw * .8, rh * .2,
                     rw * .8, rh * .8, rw * .2, rh * .8]})
        check("a settled photo can still be re-cropped", status == 200, body)
        status, body = post("/api/undo", {"name": again})
        check("undo works on it", status == 200, body)
        check("undo brought back the earlier session's own file",
              (outroot / "1_cropped" / again).read_bytes() == settled_files[again])
        check("and it is still recorded as decided",
              again in parchi.load_decided(outroot), parchi.load_decided(outroot))

        state = run_again(redo=True)
        check("start fresh leaves nobody alone", state.get("leftAlone") == 0,
              state.get("leftAlone"))
        check("start fresh clears the record",
              not parchi.decided_path(outroot).exists(),
              parchi.load_decided(outroot))

        page = get("/")[1].decode("utf-8")
        check("the page offers start fresh", 'id="redo"' in page)

        print("\n[make the photos smaller]")
        # The test photos are 1600x1200, so anything at or above that cap
        # would resize nothing and prove nothing.  800 bites.
        shrinkdir = work / "shrinkfolder"
        shrinkdir.mkdir(parents=True, exist_ok=True)
        picks = sorted(photos.glob("*.jpg"))[:3]
        for f in picks:
            shutil.copy2(f, shrinkdir / f.name)
        keep_bytes = {f.name: (shrinkdir / f.name).read_bytes() for f in picks}

        status, _ = post("/api/shrink", {"folder": str(work / "nope")})
        check("a folder that is not there is refused", status == 400, status)
        empty = work / "emptyfolder"; empty.mkdir(exist_ok=True)
        status, _ = post("/api/shrink", {"folder": str(empty)})
        check("a folder with no photos is refused", status == 400, status)
        status, _ = post("/api/shrink", {"folder": str(shrinkdir), "quality": 5})
        check("an impossible quality is refused", status == 400, status)

        status, body = post("/api/shrink", {"folder": str(shrinkdir),
                                            "fit": 800, "quality": 85})
        check("the job starts", status == 200, body)
        state = {}
        for _ in range(240):
            state = json.loads(get("/api/shrink/progress")[1])
            if not state.get("running"):
                break
            time.sleep(0.4)
        check("it finishes", not state.get("running") and state.get("done"), state)
        check("nothing failed", not state.get("failed") and not state.get("error"),
              (state.get("failed"), state.get("error")))
        check("it reports what it saved",
              state.get("after", 0) < state.get("before", 0),
              (state.get("before"), state.get("after")))
        check("the copies are in _smaller",
              Path(state.get("out", "")).name == parchi.SMALLER_NAME,
              state.get("out"))
        check("the originals are untouched, byte for byte",
              all((shrinkdir / n).read_bytes() == b
                  for n, b in keep_bytes.items()))
        made = sorted(Path(state["out"]).glob("*.jpg"))
        check("one copy per photo", len(made) == len(picks), len(made))
        check("no copy is over the cap",
              all(max(cv2.imread(str(m)).shape[:2]) <= 800 for m in made))
        check("a report is written beside them",
              (Path(state["out"]) / "smaller.csv").exists())
        check("the page offers the second door", 'id="goSmaller"' in get("/")[1].decode())
        check("and says where the copies go and what is safe",
              "_smaller" in get("/")[1].decode()
              and "not changed, moved or deleted" in get("/")[1].decode())

        print("\n[the run's size setting]")
        # A run with a cap small enough to bite, then the checks that matter:
        # the cropped file obeys it, and so does the photo that was merely
        # kept, because that is the path most photos take.
        status, body = post("/api/run", {"folder": str(photos), "confidence": 62,
                                         "review": 35, "margin": 1, "redo": True,
                                         "fit": 700, "quality": 85})
        check("a run with a size setting starts", status == 200, body)
        for _ in range(240):
            st8 = json.loads(get("/api/progress")[1])
            if not st8["running"] and st8["total"]:
                break
            time.sleep(0.5)
        rows = json.loads(get("/api/results")[1])["items"]
        written = []
        for i in rows:
            if i["bucket"] in parchi_buckets():
                f = photos / "_parchi" / i["bucket"] / i["name"]
                if f.exists():
                    written.append((i["name"], i["bucket"], cv2.imread(str(f))))
        over = [(n, b, im.shape) for n, b, im in written
                if im is not None and max(im.shape[:2]) > 700]
        check("every saved photo obeys the cap", not over, over[:3])
        kept = [w for w in written if w[1] != "1_cropped"]
        check("including the ones that were only copied through", kept, "none tested")
        check("the originals in the photo folder are the size they always were",
              max(cv2.imread(str(picks[0])).shape[:2]) == 1600)

        # the per photo escape hatch
        target3 = next(i for i in rows if i["bucket"] != "1_cropped")
        w3, h3 = target3["width"], target3["height"]
        status, body = post("/api/crop", {
            "name": target3["name"], "full_size": True,
            "quad": [0, 0, w3, 0, w3, h3, 0, h3]})
        check("full size for one photo is accepted", status == 200, body)
        got = cv2.imread(str(photos / "_parchi" / "1_cropped" / target3["name"]))
        check("and that one really is saved past the cap",
              got is not None and max(got.shape[:2]) > 700,
              None if got is None else got.shape)
        check("the page offers it only when the run is shrinking",
              "runFit ? '<label class=\"magnet\"><input type=\"checkbox\" id=\"fullSize\">"
              in get("/")[1].decode())

        print("\n[a window that fails must say why]")
        # Reported from the 1.2 release as "the main console is getting
        # closed".  Starting Crop when it is already running printed two
        # useful lines and exited, and because a .bat window closes the
        # instant the program ends, the operator saw a black window blink and
        # had nothing to report.  parchi.py has always waited for a keypress;
        # cropui.py never did.
        source = Path(cropui.__file__).read_text(encoding="utf-8")
        check("there is something to hold the window open",
              "def hold_window():" in source)
        check("it is only used when something went wrong",
              "if code:\n        hold_window()" in source)
        check("a normal stop still closes by itself",
              "hold_window()\n    sys.exit(code)" in source)

        # The dangerous half of this fix is hanging forever with no keyboard,
        # which is exactly how build.bat runs these suites.  Occupy the port,
        # start it with no console, and it must die on its own.
        import socket as sock
        held = sock.socket(sock.AF_INET, sock.SOCK_STREAM)
        held.setsockopt(sock.SOL_SOCKET, sock.SO_REUSEADDR, 1)
        held.bind(("127.0.0.1", PORT + 7))
        held.listen(1)
        try:
            env = dict(os.environ, CROP_UI_PORT=str(PORT + 7))
            run = subprocess.run([sys.executable, str(Path(cropui.__file__))],
                                 capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, timeout=60, env=env)
            check("it gives up instead of hanging with no keyboard",
                  run.returncode != 0, run.returncode)
            check("and it names the port", "already in use" in run.stdout,
                  run.stdout[-200:])
            check("and tells the operator what to do",
                  "STOP_Crop.bat" in run.stdout, run.stdout[-200:])
        except subprocess.TimeoutExpired:
            check("it gives up instead of hanging with no keyboard", False,
                  "still waiting after 60s")
        finally:
            held.close()

        print("\n[refusals]")
        status, _ = post("/api/crop", {"name": "../../../etc/passwd",
                                       "quad": [0, 0, 9, 0, 9, 9, 0, 9]})
        check("a path outside the chosen folder is refused", status == 404)
        status, _ = post("/api/run", {"folder": str(photos), "confidence": 50, "review": 80})
        check("contradictory thresholds refused", status == 400)

        # The page used to pin the review threshold at 35 while its slider went
        # down to 20, so every setting below 35 was refused with a raw error.
        # The page now derives it; this checks the rule it derives with is one
        # the server will actually accept, across the whole slider.
        page = get("/")[1].decode("utf-8")
        import re
        slider = re.search(r'id="conf"[^>]*min="(\d+)"[^>]*max="(\d+)"', page)
        check("the confidence slider is still on the page", slider is not None)
        rule = re.search(r'review:\s*Math\.min\((\d+),\s*Math\.max\((\d+),'
                         r'\s*confidence\s*-\s*(\d+)\)\)', page)
        check("the page derives the review threshold from the slider",
              rule is not None)
        if slider and rule:
            cap, floor, gap = (int(x) for x in rule.groups())
            lo, hi = int(slider.group(1)), int(slider.group(2))
            bad = [c for c in range(lo, hi + 1)
                   if min(cap, max(floor, c - gap)) > c]
            check("every setting the slider allows is one the server accepts",
                  not bad, bad[:8])
        status, _ = post("/api/run", {"folder": str(work / "nope"), "confidence": 62,
                                      "review": 35})
        check("missing folder refused", status == 400)

        print("\noriginals untouched:")
        originals = sorted(p.name for p in photos.iterdir() if p.is_file())
        check("no original renamed or removed", len(originals) == len(truth), originals)

        print("\n{} passed, {} failed.".format(passed, failed))
        return 1 if failed else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def parchi_buckets():
    import parchi
    return parchi.BUCKETS


if __name__ == "__main__":
    sys.exit(main())
