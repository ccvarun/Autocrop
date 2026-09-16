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

import cv2
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

        print("\n[the page itself]")
        page = get("/")[1].decode("utf-8")
        for piece, why in [
                ('id="preview"', "the After pane"),
                ('Original photo', "the Before label"),
                ('id="btnLayout"', "the side-by-side / wide toggle"),
                ('id="compare"', "the comparison overlay"),
                ('id="cmpA"', "the original half of the comparison"),
                ('id="cmpB"', "the cropped half of the comparison"),
                ('function openCompare', "opening a comparison"),
                ('async function undoCrop', "one undo for both places")]:
            check("the page still has {}".format(why), piece in page)

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

        print("\n[refusals]")
        status, _ = post("/api/crop", {"name": "../../../etc/passwd",
                                       "quad": [0, 0, 9, 0, 9, 9, 0, 9]})
        check("a path outside the chosen folder is refused", status == 404)
        status, _ = post("/api/run", {"folder": str(photos), "confidence": 50, "review": 80})
        check("contradictory thresholds refused", status == 400)
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
