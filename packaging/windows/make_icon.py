"""Draw the rune-stone mark from templates/base.html as a multi-size .ico (window, exe and shortcut icon).

  pip install pillow && python packaging/windows/make_icon.py

The result, src/dwmanager/static/dragonwilds-manager.ico, is committed; this only needs to run when the mark changes.
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[2] / "src/dwmanager/static/dragonwilds-manager.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]
STONE, EDGE, RUNE = "#2a3230", "#e8e0cf", "#e4622a"
# Same geometry as the SVG (viewBox 0 0 64 64), drawn large and scaled down for smooth edges.
STONE_PATH = [(22, 4), (42, 4), (54, 18), (52, 60), (12, 60), (10, 18)]
RUNE_LINES = [[(32, 16), (32, 50)], [(32, 22), (44, 32)], [(32, 32), (20, 42)]]


def draw(size):
    scale = 16
    big = size * scale
    k = big / 64
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    stone = [(x * k, y * k) for x, y in STONE_PATH]
    d.polygon(stone, fill=STONE, outline=EDGE, width=round(3 * k))
    for pts in RUNE_LINES:
        line = [(x * k, y * k) for x, y in pts]
        w = round(5 * k)
        d.line(line, fill=RUNE, width=w, joint="curve")
        for x, y in line:  # round caps
            d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=RUNE)
    return img.resize((size, size), Image.LANCZOS)


def main():
    frames = [draw(s) for s in SIZES]
    frames[-1].save(OUT, format="ICO", sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
