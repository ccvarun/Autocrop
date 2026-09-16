import cv2
import numpy as np
from pathlib import Path

import sys

try:
    import onnxruntime as ort
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        base_path = Path(sys._MEIPASS)
    else:
        base_path = Path(__file__).resolve().parent
    model_path = base_path / "models" / "u2netp.onnx"
    
    if model_path.exists():
        _sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        _iname = _sess.get_inputs()[0].name
        HAS_MODEL = True
    else:
        HAS_MODEL = False
except Exception:
    HAS_MODEL = False

def candidates_from_model(bgr, frame_area):
    """Proposes a candidate quad using a U-Net saliency model."""
    if not HAS_MODEL:
        return []

    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    im = cv2.resize(rgb, (320, 320), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    im = (im - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])
    x = im.transpose(2, 0, 1)[None].astype(np.float32)
    d = _sess.run(None, {_iname: x})[0][0, 0]
    d = (d - d.min()) / (d.max() - d.min() + 1e-8)
    
    sal = cv2.resize(d, (bgr.shape[1], bgr.shape[0]), interpolation=cv2.INTER_LINEAR)
    m = (sal > 0.5).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8), iterations=2)
    cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return []
        
    c = max(cs, key=cv2.contourArea)
    if cv2.contourArea(c) < 0.05 * frame_area:
        return []
        
    peri = cv2.arcLength(c, True)
    for eps in (0.02, 0.04, 0.06, 0.09):
        ap = cv2.approxPolyDP(c, eps * peri, True)
        if len(ap) == 4 and cv2.isContourConvex(ap):
            return [ap.reshape(4, 2).astype("float32")]
            
    return [cv2.boxPoints(cv2.minAreaRect(c)).astype("float32")]
