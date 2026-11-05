"""Turn the CSV from `generate_tags.py` into print-ready QR codes.

Runs on ANY computer. It needs only the CSV: no database, no Firebase keys,
no internet. Run it on a machine you trust, print, then delete the CSV.

    pip install "qrcode[pil]"
    python -m scripts.make_qr_sheet batch1.csv --out batch1.pdf
    python -m scripts.make_qr_sheet batch1.csv --out batch1.pdf --size-mm 25 --png-dir batch1_png

Result
  * batch1.pdf   A4 pages, 300 dpi, each QR with its reference printed under it
                 (SL-000001 ...). Print at 100% / "actual size", not "fit to page".
  * --png-dir    optional: one PNG per tag, if your printer wants image files.

The CSV holds the secret address of every tag. Anyone with it can claim those
tags, so keep it private and delete it once the tags are printed.
"""
import argparse
import csv
import sys

import qrcode
from qrcode.constants import ERROR_CORRECT_Q
from PIL import Image, ImageDraw, ImageFont

DPI = 300
A4_MM = (210, 297)


def mm_to_px(mm: float) -> int:
    return round(mm * DPI / 25.4)


def read_rows(path: str) -> list[tuple[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"tag_ref", "scan_url"} <= set(reader.fieldnames):
            sys.exit("The CSV must have the columns: tag_ref, scan_url")
        rows = [(r["tag_ref"].strip(), r["scan_url"].strip()) for r in reader if r["scan_url"].strip()]
    if not rows:
        sys.exit("The CSV has no tags in it.")
    return rows


def qr_image(url: str, size_px: int) -> Image.Image:
    """A sharp black-on-white QR code, with the 4-module quiet zone scanners need."""
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_Q, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").get_image().convert("RGB")
    return img.resize((size_px, size_px), Image.NEAREST)  # NEAREST keeps edges crisp


def load_font(size: int):
    for name in ("DejaVuSans.ttf", "Arial.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def build_pages(rows, size_mm=30.0, margin_mm=10.0, gap_mm=4.0, label_mm=6.0) -> list[Image.Image]:
    """Lay the tags out on A4 pages. Returns one PIL image per page."""
    page_w, page_h = mm_to_px(A4_MM[0]), mm_to_px(A4_MM[1])
    qr_px = mm_to_px(size_mm)
    cell_w = qr_px + mm_to_px(gap_mm)
    cell_h = qr_px + mm_to_px(label_mm) + mm_to_px(gap_mm)
    margin = mm_to_px(margin_mm)

    cols = max(1, (page_w - 2 * margin) // cell_w)
    per_col = max(1, (page_h - 2 * margin) // cell_h)
    per_page = cols * per_col

    font = load_font(mm_to_px(label_mm) * 2 // 3)
    pages = []

    for start in range(0, len(rows), per_page):
        page = Image.new("RGB", (page_w, page_h), "white")
        draw = ImageDraw.Draw(page)

        for index, (tag_ref, url) in enumerate(rows[start:start + per_page]):
            col, row = index % cols, index // cols
            x = margin + col * cell_w
            y = margin + row * cell_h

            page.paste(qr_image(url, qr_px), (x, y))
            draw.text((x + qr_px // 2, y + qr_px + mm_to_px(1)), tag_ref,
                      fill="black", font=font, anchor="mt")
            # light cut guide around each tag
            draw.rectangle([x - 2, y - 2, x + qr_px + 2, y + qr_px + mm_to_px(label_mm)],
                           outline=(200, 200, 200), width=1)
        pages.append(page)

    return pages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("csv", help="CSV written by generate_tags.py")
    parser.add_argument("--out", default="tags.pdf", help="PDF to write")
    parser.add_argument("--size-mm", type=float, default=30.0,
                        help="printed width of each QR code in mm (default 30, minimum 20)")
    parser.add_argument("--png-dir", help="also write one PNG per tag into this folder")
    args = parser.parse_args()

    if args.size_mm < 20:
        parser.error("--size-mm below 20 is hard to scan on a phone")

    rows = read_rows(args.csv)
    pages = build_pages(rows, size_mm=args.size_mm)
    pages[0].save(args.out, "PDF", resolution=float(DPI), save_all=True, append_images=pages[1:])

    if args.png_dir:
        import os
        os.makedirs(args.png_dir, exist_ok=True)
        size_px = mm_to_px(args.size_mm)
        for tag_ref, url in rows:
            qr_image(url, size_px).save(os.path.join(args.png_dir, f"{tag_ref}.png"), dpi=(DPI, DPI))

    print(f"{len(rows)} tags on {len(pages)} page(s) -> {args.out}")
    print("Print at 100% (actual size). Test-scan one printed tag BEFORE printing the rest.")
    print("The CSV holds every tag's secret address: delete it when the tags are printed.")


if __name__ == "__main__":
    main()