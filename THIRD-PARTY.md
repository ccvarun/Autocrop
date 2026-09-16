# Third party components

AutoCrop itself is MIT licensed. The published `.exe` files bundle the
following, and their licences travel with them.

| Component | Licence | Used for |
| :--- | :--- | :--- |
| [U-2-Net-p](https://github.com/xuebinqin/U-2-Net) (`models/u2netp.onnx`) | Apache 2.0 | One of the candidate generators. The ONNX file is the one published in the [rembg](https://github.com/danielgatis/rembg) releases. |
| [OpenCV](https://opencv.org) (`opencv-python-headless`) | Apache 2.0 | Edge detection, perspective warp, image IO |
| [NumPy](https://numpy.org) | BSD 3-Clause | Arrays and scoring |
| [Pillow](https://python-pillow.org) | MIT-CMU | Reading photos, EXIF rotation |
| [FastAPI](https://fastapi.tiangolo.com) | MIT | The local review server |
| [Uvicorn](https://www.uvicorn.org) | BSD 3-Clause | Serving it |
| [Pydantic](https://docs.pydantic.dev) | MIT | Request validation |
| [ONNX Runtime](https://onnxruntime.ai) | MIT | Running the saliency model |
| [img2pdf](https://gitlab.mister-muffin.de/josch/img2pdf) | LGPL 3.0 | Optional PDF export |
| [PyInstaller](https://pyinstaller.org) | GPL 2.0 with a bundling exception | Building the `.exe` files |

Two notes worth knowing before redistributing a build:

`img2pdf` is LGPL 3.0 and is linked into the `.exe`. Publishing this source
repository alongside every binary release is what keeps that straightforward:
anyone who wants to substitute their own build of `img2pdf` has everything
they need to rebuild the exe from source.

The U-2-Net model weights are research weights released under Apache 2.0.
Keep the attribution above with any redistribution.
