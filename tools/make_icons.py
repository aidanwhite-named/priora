"""엠블럼(patsearch/static/emblem.svg)과 같은 그림을 favicon.ico / apple-touch-icon.png 로 만든다.

엠블럼을 고쳤을 때만 다시 돌리면 된다. Pillow 가 필요하다(프로그램 실행에는 필요 없다).
    python tools\\make_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

STATIC = Path(__file__).resolve().parent.parent / "patsearch" / "static"
S = 16  # 64 단위 좌표를 16배로 그린 뒤 줄여서 가장자리를 부드럽게 한다
SIZE = 64 * S


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def line(draw, x1, y1, x2, y2, width, fill):
    """끝이 둥근 선."""
    w = width * S
    draw.line([(x1 * S, y1 * S), (x2 * S, y2 * S)], fill=fill, width=round(w))
    for x, y in ((x1, y1), (x2, y2)):
        draw.ellipse([x * S - w / 2, y * S - w / 2, x * S + w / 2, y * S + w / 2], fill=fill)


def render() -> Image.Image:
    top, bottom = (0x2F, 0x6F, 0xE0), (0x17, 0x3F, 0x96)
    gradient = Image.new("RGB", (SIZE, SIZE))
    gd = ImageDraw.Draw(gradient)
    for d in range(2 * SIZE):  # 대각선 방향 그라데이션
        gd.line([(d, 0), (0, d)], fill=lerp(top, bottom, d / (2 * SIZE - 1)), width=2)
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=14 * S, fill=255)
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    img.paste(gradient, (0, 0), mask)

    # 렌즈 안쪽의 옅은 흰색
    lens = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(lens).ellipse([11.5 * S, 11.5 * S, 42.5 * S, 42.5 * S], fill=(255, 255, 255, 31))
    img = Image.alpha_composite(img, lens)

    draw = ImageDraw.Draw(img)
    white, amber = (255, 255, 255, 255), (0xFF, 0xC5, 0x4D, 255)
    r, sw = 15.5, 4.5
    draw.ellipse([(27 - r - sw / 2) * S, (27 - r - sw / 2) * S, (27 + r + sw / 2) * S, (27 + r + sw / 2) * S],
                 outline=white, width=round(sw * S))
    line(draw, 19, 21.5, 35, 21.5, 3, white)
    line(draw, 19, 32.5, 30, 32.5, 3, white)
    line(draw, 19, 27, 32, 27, 3.4, amber)
    line(draw, 39, 39, 52, 52, 6.5, white)
    return img


def main() -> None:
    STATIC.mkdir(parents=True, exist_ok=True)
    img = render()
    sizes = [16, 24, 32, 48, 64, 128, 256]
    img.resize((256, 256), Image.LANCZOS).save(STATIC / "favicon.ico", sizes=[(s, s) for s in sizes])
    img.resize((180, 180), Image.LANCZOS).save(STATIC / "apple-touch-icon.png")
    (STATIC / "favicon.svg").write_bytes((STATIC / "emblem.svg").read_bytes())
    print("만든 파일:", ", ".join(p.name for p in sorted(STATIC.iterdir())))


if __name__ == "__main__":
    main()
