"""Validate and re-encode an uploaded photo (pure Pillow, no Firebase).

Why re-encode instead of storing the upload as it came in?
  * It proves the file really IS an image (the browser's file type and file
    name can be faked).
  * Phone photos carry hidden metadata, often including GPS coordinates of
    where the photo was taken (a vehicle photographed at home reveals the
    home address). Re-encoding drops all of it.
  * It caps the size, so storage and bandwidth stay small.
"""

import io

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_SIDE = 1600            # longest edge in pixels after resizing
JPEG_QUALITY = 85
MAX_PIXELS = 40_000_000    # refuse "decompression bomb" images
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def clean_image(raw: bytes) -> bytes:
    """Return a clean JPEG, or raise ValueError with a user-friendly message."""
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.format not in ALLOWED_FORMATS:
                raise ValueError("Only JPG, PNG or WebP photos are allowed.")

            img.load()  # fully decode now, so corrupt files fail here
            img = ImageOps.exif_transpose(img)  # apply rotation BEFORE metadata is dropped

            # JPEG has no transparency: flatten onto white.
            if img.mode in ("RGBA", "LA") or "transparency" in img.info:
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            img.thumbnail((MAX_SIDE, MAX_SIDE))

            out = io.BytesIO()
            # No `exif=` argument, so none of the original metadata is kept.
            img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            return out.getvalue()
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("That file is not a valid image.")