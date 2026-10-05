"""Clean studio-style packshots for the Product Master List.

Every image is an 800x800 JPEG on a pure white background with the pack
centred and a soft contact shadow, so the list, the storefront tiles and the
product page all show products at one consistent size and framing. The pack
shape (can, pouch, bottle, box, sack, ...) and its colours come from the
catalog's ``look`` tuple; the printed text is the product's own brand,
variant and net content.
"""
import io
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

CANVAS = 800
SS = 4                      # supersampling factor for smooth edges
FILL = 0.82                 # share of the frame the pack (and its shadow) may occupy
W = CANVAS * SS

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "DejaVuSans-Bold.ttf",
]


def _font(size):
    size = max(8, int(size))
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _rgb(hex_colour):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _lum(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def _s(v):
    return int(v * SS)


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------
def _shade_mask(size, bbox, mask, strength=0.55, highlight=0.35, vertical=False):
    """RGBA overlay that darkens the edges and adds a soft highlight, clipped
    to ``mask``. Gives flat shapes a rounded, three-dimensional look."""
    x0, y0, x1, y1 = bbox
    span = max(2, x1 - x0)
    dark_px, light_px = [], []
    for i in range(span):
        t = i / (span - 1)
        edge = abs(t - 0.5) * 2            # 0 centre, 1 edge
        dark_px.append(int(255 * strength * (edge ** 2.4)))
        d = (t - 0.30) / 0.16
        light_px.append(int(255 * highlight * max(0.0, 1 - d * d)))
    dark = Image.new("L", (span, 1))
    dark.putdata(dark_px)
    light = Image.new("L", (span, 1))
    light.putdata(light_px)
    dark = dark.resize((x1 - x0, max(1, y1 - y0)))
    light = light.resize((x1 - x0, max(1, y1 - y0)))
    dark_full = Image.new("L", size, 0)
    dark_full.paste(dark, (x0, y0))
    light_full = Image.new("L", size, 0)
    light_full.paste(light, (x0, y0))
    black = Image.new("RGBA", size, (0, 0, 0, 0))
    black.putalpha(ImageChops.multiply(dark_full, mask))
    white = Image.new("RGBA", size, (255, 255, 255, 0))
    white.putalpha(ImageChops.multiply(light_full, mask))
    return Image.alpha_composite(black, white)


_SHADOWS = {"on": True}


def _shadow(img, cx, base_y, width, depth=26, opacity=70):
    if not _SHADOWS["on"]:
        return
    layer = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(layer)
    d.ellipse((_s(cx - width / 2), _s(base_y - depth / 2),
               _s(cx + width / 2), _s(base_y + depth / 2)), fill=opacity)
    layer = layer.filter(ImageFilter.GaussianBlur(_s(14)))
    grey = Image.new("RGB", img.size, (60, 60, 60))
    img.paste(grey, (0, 0), layer)



def _on(c, preferred=None):
    """Readable ink colour for text printed on background ``c``."""
    if preferred is not None and abs(_lum(preferred) - _lum(c)) > 95:
        return preferred
    return (28, 28, 28) if _lum(c) > 150 else (255, 255, 255)


def _clipped_layer(img, mask, base, painter):
    """Fill ``mask`` with ``base`` and let ``painter`` decorate it, so nothing
    can spill outside the pack silhouette."""
    layer = Image.new("RGB", img.size, base)
    painter(ImageDraw.Draw(layer))
    img.paste(layer, (0, 0), mask)


def _fit_text(draw, text, max_w, start, bold=True):
    size = start
    while size > 10:
        f = _font(size)
        w = draw.textlength(text, font=f)
        if w <= max_w:
            return f
        size -= 2
    return _font(10)


def _text(draw, cx, cy, text, max_w, size, fill, outline=None):
    f = _fit_text(draw, text, _s(max_w), _s(size))
    if outline:
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-2, -2), (2, 2), (-2, 2), (2, -2)):
            draw.text((_s(cx) + dx * SS // 2, _s(cy) + dy * SS // 2), text, font=f,
                      fill=outline, anchor="mm")
    draw.text((_s(cx), _s(cy)), text, font=f, fill=fill, anchor="mm")


def _clip_paste(img, overlay, mask):
    img.paste(overlay.convert("RGB"), (0, 0), overlay.getchannel("A"))


def _round_mask(size, box, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=radius, fill=255)
    return m


# --------------------------------------------------------------------------
# pack shapes — each draws centred on an 800 canvas (coordinates in 800 space)
# --------------------------------------------------------------------------
def _can(img, look):
    shape, brand, l1, l2, size, c1, c2, tc = look
    c1, c2, tc = _rgb(c1), _rgb(c2), _rgb(tc)
    d = ImageDraw.Draw(img)
    cx, top, bot, half = 400, 215, 640, 150
    _shadow(img, cx, bot + 12, half * 2.1)
    body = (_s(cx - half), _s(top), _s(cx + half), _s(bot))
    mask = Image.new("L", img.size, 0)
    md = ImageDraw.Draw(mask)
    md.rectangle((body[0], body[1], body[2], body[3] - _s(14)), fill=255)
    md.ellipse((body[0], body[3] - _s(34), body[2], body[3] + _s(2)), fill=255)
    # label
    d.rectangle((body[0], body[1], body[2], body[3] - _s(14)), fill=c1)
    d.ellipse((body[0], body[3] - _s(34), body[2], body[3] + _s(2)), fill=c1)
    # secondary stripe bands
    d.rectangle((body[0], _s(top + 150), body[2], _s(top + 188)), fill=c2)
    # metal rims
    metal = (196, 200, 206)
    d.rectangle((body[0], body[1], body[2], _s(top + 26)), fill=metal)
    d.rectangle((body[0], _s(bot - 40), body[2], _s(bot - 14)), fill=metal)
    d.ellipse((body[0], body[3] - _s(34), body[2], body[3] + _s(2)), outline=metal, width=_s(3))
    overlay = _shade_mask(img.size, (body[0], body[1], body[2], body[3] + _s(2)), mask, 0.55, 0.45)
    _clip_paste(img, overlay, mask)
    d = ImageDraw.Draw(img)
    # lid
    d.ellipse((body[0], _s(top - 26), body[2], _s(top + 26)), fill=(222, 225, 230), outline=(160, 165, 172), width=_s(3))
    d.ellipse((body[0] + _s(22), _s(top - 15), body[2] - _s(22), _s(top + 15)), outline=(176, 181, 188), width=_s(3))
    d.rounded_rectangle((_s(cx - 34), _s(top - 10), _s(cx + 34), _s(top + 10)), radius=_s(9), outline=(150, 155, 162), width=_s(3), fill=(205, 209, 215))
    # printed text
    dark = _lum(c1) > 170
    col = (30, 30, 30) if dark else tc
    _text(d, cx, top + 70, brand, 250, 62, col, None)
    _text(d, cx, top + 118, l1, 250, 36, col)
    _text(d, cx, top + 169, l2, 250, 26, tc if _lum(c2) < 150 else (40, 30, 20))
    _text(d, cx, bot - 62, size, 200, 32, col)


def _pouch_outline(cx, top, bot, half, bulge):
    pts = []
    n = 24
    for i in range(n + 1):
        t = i / n
        y = top + (bot - top) * t
        w = half + bulge * (1 - (2 * t - 1) ** 2)
        pts.append((cx - w, y))
    for i in range(n, -1, -1):
        t = i / n
        y = top + (bot - top) * t
        w = half + bulge * (1 - (2 * t - 1) ** 2)
        pts.append((cx + w, y))
    return [(_s(x), _s(y)) for x, y in pts]


def _crimp(d, x0, x1, y0, y1, col):
    d.rectangle((_s(x0), _s(y0), _s(x1), _s(y1)), fill=col)
    step = 9
    x = x0
    dark = _mix(col, (0, 0, 0), 0.18)
    while x < x1:
        d.line((_s(x), _s(y0), _s(x), _s(y1)), fill=dark, width=_s(2))
        x += step


def _pouch(img, look, wide=False, puffed=False, tall=False):
    shape, brand, l1, l2, size, c1, c2, tc = look
    c1, c2, tc = _rgb(c1), _rgb(c2), _rgb(tc)
    cx = 400
    top, bot = (130, 670) if tall else (170, 640)
    half = 190 if wide else 150
    bulge = 26 if puffed else 14
    _shadow(img, cx, bot + 10, half * 2.0)
    pts = _pouch_outline(cx, top, bot, half, bulge)
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    band_y = top + (bot - top) * 0.60

    def paint(ld):
        ld.polygon([(_s(cx - half - 60), _s(band_y)), (_s(cx), _s(band_y - 46)),
                    (_s(cx + half + 60), _s(band_y)), (_s(cx + half + 60), _s(bot + 40)),
                    (_s(cx - half - 60), _s(bot + 40))], fill=c2)
    _clipped_layer(img, mask, c1, paint)
    overlay = _shade_mask(img.size, (_s(cx - half - bulge), _s(top), _s(cx + half + bulge), _s(bot)), mask, 0.42, 0.30)
    _clip_paste(img, overlay, mask)
    d = ImageDraw.Draw(img)
    crimp_col = _mix(c1, (255, 255, 255), 0.18)
    _crimp(d, cx - half, cx + half, top, top + 26, crimp_col)
    _crimp(d, cx - half, cx + half, bot - 26, bot, crimp_col)
    area = half * 1.7
    col = _on(c1, tc)
    _text(d, cx, top + 98, brand, area, 56, col)
    _text(d, cx, top + 160, l1, area, 38, col)
    _text(d, cx, top + 205, l2, area, 28, col)
    _text(d, cx, band_y + 62, size, area, 40, _on(c2))

def _box(img, look, flat=False):
    shape, brand, l1, l2, size, c1, c2, tc = look
    c1, c2, tc = _rgb(c1), _rgb(c2), _rgb(tc)
    d = ImageDraw.Draw(img)
    x0, x1 = (190, 590) if not flat else (150, 600)
    y0, y1 = (160, 640) if not flat else (250, 600)
    depth = 70
    _shadow(img, (x0 + x1) / 2 + depth / 2, y1 + 14, (x1 - x0) + depth + 30)
    # side face
    side = _mix(c1, (0, 0, 0), 0.35)
    d.polygon([(_s(x1), _s(y0)), (_s(x1 + depth), _s(y0 - 34)), (_s(x1 + depth), _s(y1 - 34)), (_s(x1), _s(y1))], fill=side)
    # top face
    top = _mix(c1, (255, 255, 255), 0.28)
    d.polygon([(_s(x0), _s(y0)), (_s(x0 + depth), _s(y0 - 34)), (_s(x1 + depth), _s(y0 - 34)), (_s(x1), _s(y0))], fill=top)
    # front face
    front = (_s(x0), _s(y0), _s(x1), _s(y1))
    d.rectangle(front, fill=c1)
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rectangle(front, fill=255)
    d.rectangle((_s(x0), _s(y0 + (y1 - y0) * 0.66), _s(x1), _s(y1)), fill=c2)
    overlay = _shade_mask(img.size, front, mask, 0.28, 0.22)
    _clip_paste(img, overlay, mask)
    d = ImageDraw.Draw(img)
    cx = (x0 + x1) / 2
    light = _lum(c1) > 170
    col = (30, 30, 30) if light else tc
    area = (x1 - x0) * 0.86
    _text(d, cx, y0 + 90, brand, area, 62, col)
    _text(d, cx, y0 + 152, l1, area, 38, col)
    _text(d, cx, y0 + 198, l2, area, 28, col)
    sec_txt = (255, 255, 255) if _lum(c2) < 160 else (40, 30, 20)
    _text(d, cx, y0 + (y1 - y0) * 0.66 + (y1 - y0) * 0.17, size, area, 40, sec_txt)


def _bottle(img, look):
    shape, brand, l1, l2, size, c1, c2, tc = look
    c1, c2, tc = _rgb(c1), _rgb(c2), _rgb(tc)
    cx = 400
    big = size in ("1.5 L", "1 L")
    half = 128 if big else 106
    top, bot = (110, 650) if big else (170, 640)
    shoulder = top + 120
    neck = 36
    _shadow(img, cx, bot + 10, half * 2.2)
    d = ImageDraw.Draw(img)
    # liquid colour by product
    liquid = {
        "Coca-Cola": (52, 22, 14), "SILVER SWAN": (55, 30, 16), "DATU PUTI": (236, 222, 160),
        "UFC": (190, 28, 36), "H&S": (96, 190, 226),
    }.get(brand, c1)
    glass = _mix(liquid, (255, 255, 255), 0.0)
    outline = [(cx - neck, top + 40), (cx - neck, shoulder - 40), (cx - half, shoulder + 30),
               (cx - half, bot - 24), (cx - half + 24, bot), (cx + half - 24, bot), (cx + half, bot - 24),
               (cx + half, shoulder + 30), (cx + neck, shoulder - 40), (cx + neck, top + 40)]
    pts = [(_s(x), _s(y)) for x, y in outline]
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    d.polygon(pts, fill=glass)
    # label
    lab_top, lab_bot = shoulder + 40, bot - 56
    lab = (_s(cx - half), _s(lab_top), _s(cx + half), _s(lab_bot))
    lab_col = c1 if brand not in ("Coca-Cola", "SILVER SWAN", "UFC", "H&S") else c1
    if brand == "UFC":
        lab_col = (250, 235, 190)
    if brand == "SILVER SWAN":
        lab_col = (245, 240, 225)
    if brand == "DATU PUTI":
        lab_col = (250, 250, 245)
    if brand == "H&S":
        lab_col = (255, 255, 255)
    d.rectangle(lab, fill=lab_col)
    accent = c2
    d.rectangle((lab[0], lab[1], lab[2], _s(lab_top + 16)), fill=accent)
    d.rectangle((lab[0], lab[3] - _s(16), lab[2], lab[3]), fill=accent)
    overlay = _shade_mask(img.size, (_s(cx - half), _s(top), _s(cx + half), _s(bot)), mask, 0.50, 0.50)
    _clip_paste(img, overlay, mask)
    d = ImageDraw.Draw(img)
    # cap
    cap_col = {"Coca-Cola": (200, 20, 30), "SILVER SWAN": (200, 30, 36), "DATU PUTI": (30, 120, 60),
               "UFC": (200, 30, 40), "H&S": (240, 240, 245)}.get(brand, c2)
    d.rounded_rectangle((_s(cx - neck - 6), _s(top - 4), _s(cx + neck + 6), _s(top + 52)), radius=_s(8), fill=cap_col)
    for i in range(10):
        x = cx - neck + 6 + i * 6.4
        d.line((_s(x), _s(top + 2), _s(x), _s(top + 46)), fill=_mix(cap_col, (0, 0, 0), 0.25), width=_s(2))
    # label text
    lab_light = _lum(lab_col) > 150
    col = (30, 30, 30) if lab_light else tc
    if brand == "Coca-Cola":
        col = (255, 255, 255)
    if brand in ("SILVER SWAN", "UFC", "DATU PUTI"):
        col = {"SILVER SWAN": (150, 24, 20), "UFC": (170, 20, 30), "DATU PUTI": (22, 100, 48)}[brand]
    if brand == "H&S":
        col = (22, 120, 190)
    area = half * 1.7
    mid = (lab_top + lab_bot) / 2
    _text(d, cx, mid - 46, brand, area, 60, col)
    _text(d, cx, mid + 12, l1, area, 40, col)
    _text(d, cx, mid + 56, l2, area, 28, col)
    sz_col = (255, 255, 255) if _lum(accent) < 150 else (40, 30, 20)
    _text(d, cx, lab_bot - 34, size, area * 0.7, 28, col)


def _bigpack(img, look):
    shape, brand, l1, l2, size, c1, c2, tc = look
    c1, c2, tc = _rgb(c1), _rgb(c2), _rgb(tc)
    x0, x1, y0, y1 = 170, 630, 150, 660
    _shadow(img, 400, y1 + 12, 480)
    box = (_s(x0), _s(y0), _s(x1), _s(y1))
    mask = _round_mask(img.size, box, _s(46))

    def paint(ld):
        ld.pieslice((_s(x0 - 200), _s(y0 + 330), _s(x1 + 200), _s(y1 + 380)), 180, 360, fill=c2)
        ld.ellipse((_s(310), _s(y0 + 150), _s(490), _s(y0 + 330)), fill=(255, 255, 255))
        ld.ellipse((_s(335), _s(y0 + 175), _s(465), _s(y0 + 305)), fill=_mix(c1, (255, 255, 255), 0.65))
        ld.ellipse((_s(368), _s(y0 + 197), _s(432), _s(y0 + 261)), fill=(250, 214, 178))
        ld.rounded_rectangle((_s(320), _s(y0 + 24), _s(480), _s(y0 + 54)), radius=_s(15), fill=(255, 255, 255))
    _clipped_layer(img, mask, c1, paint)
    overlay = _shade_mask(img.size, box, mask, 0.34, 0.28)
    _clip_paste(img, overlay, mask)
    d = ImageDraw.Draw(img)
    _text(d, 400, y0 + 106, brand, 340, 70, (255, 255, 255))
    _text(d, 400, y0 + 372, l1, 340, 44, (24, 100, 130))
    _text(d, 400, y0 + 420, l2, 340, 34, (24, 100, 130))
    _text(d, 400, y0 + 468, size, 340, 36, (30, 70, 90))

def _sachet(img, look):
    """One sachet, or a strip of three when the pack holds several."""
    multi = "sachets" in look[4] or " x " in look[4].lower() or "x" in look[4].split()
    if multi:
        _single_sachet(img, look, 262, 0.80, 100, text=False)
        _single_sachet(img, look, 538, 0.80, 100, text=False)
        _single_sachet(img, look, 400, 0.92, 128, text=True)
    else:
        _single_sachet(img, look, 400, 1.0, 165, text=True)

def _single_sachet(img, look, cx, scale, half_w, text=True):
    shape, brand, l1, l2, size, c1, c2, tc = look
    c1, c2, tc = _rgb(c1), _rgb(c2), _rgb(tc)
    mid = 410
    top, bot = mid - 240 * scale, mid + 240 * scale
    half = half_w
    _shadow(img, cx, bot + 10, half * 2.0, opacity=55)
    pts = _pouch_outline(cx, top, bot, half, 10)
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    by = top + (bot - top) * 0.62

    def paint(ld):
        ld.polygon([(_s(cx - half - 30), _s(by + 24)), (_s(cx + half + 30), _s(by - 24)),
                    (_s(cx + half + 30), _s(bot + 30)), (_s(cx - half - 30), _s(bot + 30))], fill=c2)
    _clipped_layer(img, mask, c1, paint)
    overlay = _shade_mask(img.size, (_s(cx - half - 10), _s(top), _s(cx + half + 10), _s(bot)), mask, 0.38, 0.30)
    _clip_paste(img, overlay, mask)
    d = ImageDraw.Draw(img)
    _crimp(d, cx - half, cx + half, top, top + 22 * scale, _mix(c1, (255, 255, 255), 0.2))
    _crimp(d, cx - half, cx + half, bot - 22 * scale, bot, _mix(c1, (255, 255, 255), 0.2))
    if not text:
        return
    col = _on(c1, tc)
    area = half * 1.75
    _text(d, cx, top + 96 * scale, brand, area, 52 * scale, col)
    _text(d, cx, top + 158 * scale, l1, area, 36 * scale, col)
    _text(d, cx, top + 202 * scale, l2, area, 26 * scale, col)
    _text(d, cx, bot - 74 * scale, size, area, 34 * scale, _on(c2))

def _ricepack(img, look):
    shape, brand, l1, l2, size, c1, c2, tc = look
    c1, c2, tc = _rgb(c1), _rgb(c2), _rgb(tc)
    d = ImageDraw.Draw(img)
    cx, top, bot, half = 400, 150, 660, 168
    _shadow(img, cx, bot + 10, half * 2.2)
    pts = _pouch_outline(cx, top, bot, half, 16)
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    d.polygon(pts, fill=c1)
    # clear window showing grain
    win = (_s(cx - half + 30), _s(top + 300), _s(cx + half - 30), _s(bot - 70))
    d.rounded_rectangle(win, radius=_s(26), fill=(244, 238, 222))
    import random
    rnd = random.Random(7)
    for _ in range(260):
        gx = rnd.randint(win[0] + 10, win[2] - 10)
        gy = rnd.randint(win[1] + 10, win[3] - 10)
        d.ellipse((gx, gy, gx + _s(9), gy + _s(3.4)), fill=(255, 253, 246), outline=(226, 218, 196))
    overlay = _shade_mask(img.size, (_s(cx - half - 16), _s(top), _s(cx + half + 16), _s(bot)), mask, 0.40, 0.35)
    _clip_paste(img, overlay, mask)
    d = ImageDraw.Draw(img)
    _crimp(d, cx - half, cx + half, top, top + 26, _mix(c1, (255, 255, 255), 0.2))
    _crimp(d, cx - half, cx + half, bot - 26, bot, _mix(c1, (255, 255, 255), 0.2))
    d.rounded_rectangle((_s(cx - half + 30), _s(top + 56), _s(cx + half - 30), _s(top + 280)), radius=_s(18), fill=c2)
    _text(d, cx, top + 112, brand, 230, 40, (40, 70, 40))
    _text(d, cx, top + 160, l1, 230, 34, (40, 70, 40))
    _text(d, cx, top + 205, l2, 230, 22, (60, 90, 60))
    _text(d, cx, top + 250, size, 230, 32, (40, 70, 40))


def _sack(img, look):
    shape, brand, l1, l2, size, c1, c2, tc = look
    c1, c2, tc = _rgb(c1), _rgb(c2), _rgb(tc)
    cx, top, bot, half = 400, 120, 670, 200
    _shadow(img, cx, bot + 8, half * 2.3)
    pts = []
    for i in range(0, 41):
        t = i / 40
        w = half * (0.62 + 0.38 * min(1, t * 3.2)) + 20 * (1 - (2 * t - 1) ** 2)
        pts.append((cx - w, top + (bot - top) * t))
    for i in range(40, -1, -1):
        t = i / 40
        w = half * (0.62 + 0.38 * min(1, t * 3.2)) + 20 * (1 - (2 * t - 1) ** 2)
        pts.append((cx + w, top + (bot - top) * t))
    sp = [(_s(x), _s(y)) for x, y in pts]
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).polygon(sp, fill=255)

    def paint(ld):
        weave = _mix(c1, (0, 0, 0), 0.10)
        for y in range(int(top), int(bot), 9):
            ld.line((_s(cx - 260), _s(y), _s(cx + 260), _s(y)), fill=weave, width=_s(2))
        for x in range(int(cx - 260), int(cx + 260), 9):
            ld.line((_s(x), _s(top), _s(x), _s(bot)), fill=_mix(c1, (255, 255, 255), 0.10), width=_s(1))
        ld.rounded_rectangle((_s(cx - 150), _s(top + 190), _s(cx + 150), _s(top + 440)), radius=_s(20),
                             fill=(250, 244, 230), outline=c2, width=_s(8))
        ld.rectangle((_s(cx - 70), _s(top + 52), _s(cx + 70), _s(top + 74)), fill=c2)
    _clipped_layer(img, mask, c1, paint)
    overlay = _shade_mask(img.size, (_s(cx - half - 30), _s(top), _s(cx + half + 30), _s(bot)), mask, 0.36, 0.26)
    _clip_paste(img, overlay, mask)
    d = ImageDraw.Draw(img)
    _crimp(d, cx - 100, cx + 100, top, top + 38, _mix(c1, (0, 0, 0), 0.12))
    _text(d, cx, top + 245, brand, 250, 50, c2)
    _text(d, cx, top + 305, l1, 250, 58, c2)
    _text(d, cx, top + 360, l2, 250, 28, (90, 60, 40))
    _text(d, cx, top + 405, size, 250, 40, c2)

def _cup(img, look):
    shape, brand, l1, l2, size, c1, c2, tc = look
    c1, c2, tc = _rgb(c1), _rgb(c2), _rgb(tc)
    d = ImageDraw.Draw(img)
    cx, top, bot = 400, 250, 640
    tw, bw = 200, 140
    _shadow(img, cx, bot + 10, tw * 2)
    pts = [(_s(cx - tw), _s(top)), (_s(cx + tw), _s(top)), (_s(cx + bw), _s(bot)), (_s(cx - bw), _s(bot))]
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    d.polygon(pts, fill=c1)
    d.polygon([(_s(cx - 176), _s(top + 210)), (_s(cx + 176), _s(top + 210)), (_s(cx + bw), _s(bot)), (_s(cx - bw), _s(bot))], fill=c2)
    overlay = _shade_mask(img.size, (_s(cx - tw), _s(top), _s(cx + tw), _s(bot)), mask, 0.45, 0.35)
    _clip_paste(img, overlay, mask)
    d = ImageDraw.Draw(img)
    d.ellipse((_s(cx - tw - 12), _s(top - 22), _s(cx + tw + 12), _s(top + 22)), fill=(236, 236, 240), outline=(170, 170, 178), width=_s(3))
    d.ellipse((_s(cx - tw + 14), _s(top - 12), _s(cx + tw - 14), _s(top + 12)), fill=(214, 216, 222))
    _text(d, cx, top + 70, brand, 300, 56, (255, 255, 255))
    _text(d, cx, top + 124, l1, 300, 36, (255, 255, 255))
    _text(d, cx, top + 165, l2, 300, 30, (255, 255, 255))
    _text(d, cx, top + 292, size, 190, 34, (60, 20, 15))


def _soapbox(img, look):
    shape, brand, l1, l2, size, c1, c2, tc = look
    c2 = _rgb(c2)
    d = ImageDraw.Draw(img)
    x0, x1, y0, y1 = 150, 540, 190, 520
    depth = 80
    _shadow(img, 440, 665, 560)
    d.polygon([(_s(x1), _s(y0)), (_s(x1 + depth), _s(y0 - 40)), (_s(x1 + depth), _s(y1 - 40)), (_s(x1), _s(y1))], fill=(196, 207, 221))
    d.polygon([(_s(x0), _s(y0)), (_s(x0 + depth), _s(y0 - 40)), (_s(x1 + depth), _s(y0 - 40)), (_s(x1), _s(y0))], fill=(240, 244, 249))
    d.rectangle((_s(x0), _s(y0), _s(x1), _s(y1)), fill=(252, 252, 254), outline=(214, 220, 228), width=_s(3))
    d.rectangle((_s(x0), _s(y0 + 130), _s(x1), _s(y0 + 196)), fill=c2)
    d.rectangle((_s(x0), _s(y1 - 26), _s(x1), _s(y1)), fill=c2)
    cx = (x0 + x1) / 2
    _text(d, cx, y0 + 66, brand, 320, 66, c2)
    _text(d, cx, y0 + 163, l1, 320, 46, (255, 255, 255))
    _text(d, cx, y0 + 240, l2, 320, 34, c2)
    _text(d, cx, y0 + 288, size, 320, 32, (70, 90, 120))
    # the bar in front of the box
    bar = (_s(240), _s(550), _s(560), _s(650))
    d.rounded_rectangle(bar, radius=_s(36), fill=(250, 251, 253), outline=(206, 214, 226), width=_s(3))
    d.rounded_rectangle((_s(268), _s(568), _s(532), _s(632)), radius=_s(26), outline=(220, 227, 237), width=_s(3))
    _text(d, 400, 600, brand, 200, 34, (110, 140, 190))

_DRAW = {
    "can": _can,
    "pouch": lambda i, l: _pouch(i, l),
    "chips": lambda i, l: _pouch(i, l, wide=True, puffed=True, tall=True),
    "box": lambda i, l: _box(i, l),
    "flatpack": lambda i, l: _box(i, l, flat=True),
    "bottle": _bottle,
    "bigpack": _bigpack,
    "sachet": _sachet,
    "ricepack": _ricepack,
    "sack": _sack,
    "cup": _cup,
    "soapbox": _soapbox,
}


def _fit_to_frame(img):
    """Crop to the artwork and re-centre it so every pack, whatever its shape,
    fills the same share of the frame. Without this a squat can sits in a sea
    of white while a tall bottle nearly touches the edges. Returns the fitted
    image and the (left, top, right, bottom) box the pack now occupies."""
    white = Image.new("RGB", img.size, (255, 255, 255))
    ink = ImageChops.difference(img, white).convert("L").point(lambda v: 255 if v > 12 else 0)
    box = ink.getbbox()
    if not box:
        return img, (0, 0, img.size[0], img.size[1])
    art = img.crop(box)
    target = int(img.size[0] * FILL)
    scale = min(target / art.width, target / art.height)
    art = art.resize((max(1, int(art.width * scale)), max(1, int(art.height * scale))), Image.LANCZOS)
    out = Image.new("RGB", img.size, (255, 255, 255))
    left = (img.size[0] - art.width) // 2
    top = (img.size[1] - art.height) // 2 - int(img.size[1] * 0.015)
    out.paste(art, (left, top))
    return out, (left, top, left + art.width, top + art.height)


def render(look, out_size=CANVAS):
    """Return a PIL RGB image (white background) for a catalog ``look``."""
    img = Image.new("RGB", (W, W), (255, 255, 255))
    _SHADOWS["on"] = False
    try:
        _DRAW[look[0]](img, look)
    finally:
        _SHADOWS["on"] = True
    fitted, (left, top, right, bottom) = _fit_to_frame(img)
    # one soft contact shadow under the whole pack, in the 800-unit space
    _shadow(fitted, (left + right) / 2 / SS, bottom / SS + 4, (right - left) / SS * 0.98, depth=30, opacity=64)
    return fitted.resize((out_size, out_size), Image.LANCZOS)


def render_jpeg_bytes(look):
    buf = io.BytesIO()
    render(look).save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()


def is_white_background(image, tolerance=6):
    """True when all four corners and edge midpoints are (near) pure white."""
    w, h = image.size
    pts = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
    rgb = image.convert("RGB")
    return all(min(rgb.getpixel(p)) >= 255 - tolerance for p in pts)
