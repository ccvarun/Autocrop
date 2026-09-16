"""
pdfout.py : put finished pages into a PDF.

Kept in its own file so that work on detection and work on output do not land
in the same place.
"""

from pathlib import Path

PAGE_SIZES = {"a4": (210.0, 297.0), "letter": (215.9, 279.4)}


def build_pdf(images, out_path, page_size=None):
    """Combine images into one PDF, in the order given.

    A document scanner's output is a PDF, not a pile of JPEGs.  img2pdf embeds
    each JPEG exactly as it is with no re-encoding, so nothing is lost a second
    time: every extra generation of JPEG makes faint pen harder to read.

    Returns (path, message) on success, or (None, why) if it could not.
    """
    images = [Path(p) for p in images]
    usable = [p for p in images
              if p.exists() and p.suffix.lower() in (".jpg", ".jpeg")]
    skipped = len(images) - len(usable)
    if not usable:
        return None, "no pages to put in a PDF"

    try:
        import img2pdf
    except ImportError:
        return None, "img2pdf is not installed"

    options = {}
    if page_size:
        size = PAGE_SIZES.get(str(page_size).lower())
        if size is None:
            return None, "unknown page size: {}".format(page_size)
        # Only pass a layout when one was asked for.  img2pdf treats an
        # explicit layout_fun=None as a callable and fails on it.
        options["layout_fun"] = img2pdf.get_layout_fun(
            (img2pdf.mm_to_pt(size[0]), img2pdf.mm_to_pt(size[1])))

    try:
        data = img2pdf.convert([str(p) for p in usable], **options)
    except Exception as err:
        return None, "could not build the PDF: {}".format(err)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)

    message = "{} pages".format(len(usable))
    if skipped:
        message += ", {} skipped (not JPEG)".format(skipped)
    return out_path, message
