"""
Cleans a photo or scan of sheet music so it looks like a flat, white, high-contrast scan.
Runs BEFORE recognition (the engine reads the clean page) and its output is also what the
app shows while playing, one line of music at a time.

Steps:
  1. find the page and flatten its perspective (phone photos)
  2. remove shadows and uneven light (divide by an estimated background)
  3. straighten the page using the staff lines
  4. measure the staff-line spacing and upscale small images
  5. levels: paper becomes pure white, ink becomes solid dark, thin lines are kept
  6. remove isolated specks (dust, paper grain) without touching dots and staccatos
  7. find the lines of music (systems) and straighten each one (bowed pages)

Only OpenCV + NumPy (Apache-2.0 / BSD), safe for a paid product.

    python cleaning/clean_page.py pages/photo.jpg out_dir/
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import numpy as np

TARGET_SPACING = 20  # px between staff lines that recognition and display both like


@dataclass
class CleanInfo:
    page_found: bool = False
    rotation_deg: float = 0.0
    spacing_before: float = 0.0
    scale: float = 1.0
    staves: list = field(default_factory=list)    # [top_y, bottom_y] of every 5-line staff
    systems: list = field(default_factory=list)   # [y0, y1] crop band of every line of music


# ---------- 1. page + perspective ----------

def _order(pts: np.ndarray) -> np.ndarray:
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], np.float32)


def flatten_page(img: np.ndarray) -> tuple[np.ndarray, bool]:
    h, w = img.shape[:2]
    k = 1000 / max(h, w)
    small = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    g = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    edges = cv2.dilate(cv2.Canny(g, 40, 120), np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        if cv2.contourArea(c) < 0.35 * small.shape[0] * small.shape[1]:
            break
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(approx) == 4:
            src = _order(approx.reshape(4, 2).astype(np.float32) / k)
            wd = int(max(np.linalg.norm(src[1] - src[0]), np.linalg.norm(src[2] - src[3])))
            ht = int(max(np.linalg.norm(src[3] - src[0]), np.linalg.norm(src[2] - src[1])))
            dst = np.array([[0, 0], [wd - 1, 0], [wd - 1, ht - 1], [0, ht - 1]], np.float32)
            return cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (wd, ht)), True
    return img, False


# ---------- 2. light ----------

def remove_shadows(gray: np.ndarray) -> np.ndarray:
    """Estimate the paper (ink removed by dilation, then a wide median) and divide it out."""
    ksize = max(7, (min(gray.shape) // 150) | 1)
    bg = cv2.medianBlur(cv2.dilate(gray, np.ones((ksize, ksize), np.uint8)), max(21, (min(gray.shape) // 25) | 1))
    norm = cv2.divide(gray, bg, scale=255)
    return norm


# ---------- 3. rotation ----------

def _ink(gray: np.ndarray) -> np.ndarray:
    return cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]


def find_rotation(gray: np.ndarray) -> float:
    """Angle that makes staff lines most horizontal (sharpest row profile)."""
    k = 1200 / max(gray.shape)
    ink = _ink(cv2.resize(gray, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)).astype(np.float32)
    h, w = ink.shape
    best, best_score = 0.0, -1.0
    for step in (0.5, 0.1):
        for a in np.arange(best - step * 6, best + step * 6 + 1e-6, step):
            m = cv2.getRotationMatrix2D((w / 2, h / 2), a, 1)
            prof = cv2.warpAffine(ink, m, (w, h)).sum(1)
            score = float((prof ** 2).sum())
            if score > best_score:
                best, best_score = float(a), score
    return round(best, 2)


def rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 0.05:
        return gray
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)


# ---------- 4. staff spacing ----------

def staff_rows(strip: np.ndarray) -> np.ndarray:
    """Rows of a narrow vertical strip that hold a staff line. On photos lines tilt or bow a
    little; over a narrow strip they are still horizontal."""
    ink = _ink(strip)
    horiz = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, strip.shape[1] // 6), 1)))
    return (horiz.sum(1) / 255) > strip.shape[1] * 0.5


def line_centers(rows: np.ndarray) -> list[float]:
    centers, start = [], None
    for y, hit in enumerate(rows):
        if hit and start is None:
            start = y
        elif not hit and start is not None:
            centers.append((start + y - 1) / 2)
            start = None
    return centers


def _staves_in(centers: list[float]) -> tuple[list[tuple[float, float]], float]:
    if len(centers) < 5:
        return [], 0.0
    gaps = np.diff(centers)
    spacing = float(np.median(np.sort(gaps)[: max(1, len(gaps) // 2)]))
    staves, run = [], [centers[0]]
    for y, g in zip(centers[1:], gaps):
        if g < spacing * 1.6:
            run.append(y)
        else:
            if 4 <= len(run) <= 6:
                staves.append((run[0], run[-1]))
            run = [y]
    if 4 <= len(run) <= 6:
        staves.append((run[0], run[-1]))
    return staves, spacing


def find_staves(gray: np.ndarray) -> tuple[list[tuple[float, float]], float]:
    """Look for 5-line staves in five strips across the page and keep the ones that at least
    two strips agree on. Notes, ledger lines or text crossing one strip can't hide a staff."""
    w = gray.shape[1]
    found, spacings = [], []
    for c in (0.2, 0.35, 0.5, 0.65, 0.8):
        strip = gray[:, int(w * (c - 0.07)): int(w * (c + 0.07))]
        st, sp = _staves_in(line_centers(staff_rows(strip)))
        found.append(st)
        if sp:
            spacings.append(sp)
    if not spacings:
        return [], 0.0
    spacing = float(np.median(spacings))
    cands = sorted((a, b) for st in found for a, b in st)
    merged = []
    for a, b in cands:
        if merged and abs((a + b) / 2 - merged[-1][2]) < spacing * 2.5:
            m = merged[-1]
            m[0].append(a); m[1].append(b); m[2] = float(np.mean([(x + y) / 2 for x, y in zip(m[0], m[1])]))
        else:
            merged.append([[a], [b], (a + b) / 2])
    staves = [(float(np.median(m[0])), float(np.median(m[1]))) for m in merged if len(m[0]) >= 2]
    return staves, spacing


# ---------- 5 + 6. levels and specks ----------

def levels(gray: np.ndarray) -> np.ndarray:
    """Paper -> 255, ink -> near black, anti-aliased edges kept so thin lines stay smooth.
    Black and white points come from the page itself, so faint or thin print (staff lines
    and small text on old or low-resolution scans) still ends up dark."""
    t = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[0]
    ink, paper = gray[gray < t], gray[gray >= t]
    black = float(np.percentile(ink, 70)) if ink.size else t * 0.5
    white = float(np.percentile(paper, 8)) if paper.size else 250.0
    white = max(white, black + 30)
    out = (np.clip((gray.astype(np.float32) - black) / (white - black), 0, 1) * 255).astype(np.uint8)
    out[out > 205] = 255  # paper grain and faint shadow become clean white
    return out


def sharpen(gray: np.ndarray, spacing: float) -> np.ndarray:
    """Unsharp mask sized to the print, used after upscaling small scans."""
    sigma = max(1.0, spacing / 12)
    blur = cv2.GaussianBlur(gray, (0, 0), sigma)
    return cv2.addWeighted(gray, 1.8, blur, -0.8, 0)


def remove_specks(gray: np.ndarray, spacing: float) -> np.ndarray:
    ink = (gray < 128).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    max_area = max(2, int((spacing * 0.12) ** 2))  # well under an augmentation dot (~ (0.4*spacing)^2)
    small = np.where(stats[:, cv2.CC_STAT_AREA] <= max_area)[0]
    small = small[small != 0]
    out = gray.copy()
    if len(small):
        out[np.isin(labels, small)] = 255
    return out


# ---------- 7. lines of music ----------

def system_bands(staves, height: int, staves_per_system: int) -> list[tuple[int, int]]:
    groups = [staves[i:i + staves_per_system] for i in range(0, len(staves), staves_per_system)]
    groups = [g for g in groups if len(g) == staves_per_system]
    bands = []
    for i, g in enumerate(groups):
        top = g[0][0] if i == 0 else (groups[i - 1][-1][1] + g[0][0]) / 2
        bottom = g[-1][1] if i == len(groups) - 1 else (g[-1][1] + groups[i + 1][0][0]) / 2
        if i == 0:
            top = max(0, g[0][0] - (g[0][1] - g[0][0]) * 1.6)
        if i == len(groups) - 1:
            bottom = min(height, g[-1][1] + (g[-1][1] - g[-1][0]) * 1.6)
        bands.append((int(top), int(bottom)))
    return bands


def guess_staves_per_system(staves) -> int:
    """Piano music: pairs of staves sit closer together than the gap to the next pair."""
    if len(staves) < 4:
        return 1
    gaps = [staves[i + 1][0] - staves[i][1] for i in range(len(staves) - 1)]
    even, odd = np.mean(gaps[0::2]), np.mean(gaps[1::2]) if len(gaps) > 1 else 0
    return 2 if odd > even * 1.35 else 1


def straighten_band(band: np.ndarray, strip: int = 48) -> np.ndarray:
    return straighten_band_with_shift(band, strip)[0]


def _column_offsets(band: np.ndarray, strip: int = 64) -> tuple[list[float], list[int]]:
    """For narrow column strips across the band: how far up or down its staff lines sit compared
    with the middle of the band. Only long horizontal segments (staff lines) are compared, and
    each strip is matched to its neighbour with less than half a line spacing of freedom, so
    the periodic staff can't be matched one line off. Bends add up from the middle outwards."""
    h, w = band.shape
    lines = cv2.morphologyEx(255 - band, cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (strip, 1))).astype(np.float32)
    _, spacing = find_staves(band)
    reach = max(2, int((spacing or TARGET_SPACING) * 0.45))

    def shifted(p, k):
        out = np.zeros_like(p)
        if k >= 0:
            out[k:] = p[:len(p) - k]
        else:
            out[:k] = p[-k:]
        return out

    starts = list(range(0, w - strip, strip // 2))
    profs = [lines[:, x0:x0 + strip].sum(1) for x0 in starts]
    good = [p.max() >= strip * 255 * 0.5 for p in profs]
    mid = min(range(len(starts)), key=lambda i: abs(starts[i] + strip / 2 - w / 2))
    offs = {mid: 0}
    for order in (range(mid + 1, len(starts)), range(mid - 1, -1, -1)):
        prev = mid
        for i in order:
            if not good[i]:
                continue
            ref = shifted(profs[prev], -offs[prev])
            corr = [float(np.dot(shifted(profs[i], -(offs[prev] + k)), ref)) for k in range(-reach, reach + 1)]
            offs[i] = offs[prev] + int(np.argmax(corr)) - reach
            prev = i
    keys = sorted(k for k in offs if good[k])
    return [starts[k] + strip / 2 for k in keys], [offs[k] for k in keys]


def straighten_band_with_shift(band: np.ndarray, strip: int = 64) -> tuple[np.ndarray, np.ndarray]:
    """Vertical-only dewarp: shift each column so staff lines become straight. x stays the same,
    so note positions from recognition still line up with the image. Returns the image and the
    per-column vertical shift (add it to an original y to find that point in the new image).
    The result is kept only if the lines really got straighter."""
    h, w = band.shape
    none = np.zeros(w, np.float32)
    xs, offs = _column_offsets(band, strip)
    if len(xs) < 6 or np.ptp(offs) < 1:
        return band, none
    fit = np.polyval(np.polyfit(xs, offs, 3), np.arange(w))
    map_x, map_y = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    map_y += fit[None, :].astype(np.float32)  # lines in column x sit fit[x] lower: read from there
    out = cv2.remap(band, map_x, map_y, cv2.INTER_CUBIC, borderValue=255)
    _, after = _column_offsets(out, strip)
    if not after or np.std(after) >= np.std(offs):
        return band, none
    return out, (-fit).astype(np.float32)


def straightness(band: np.ndarray) -> float:
    """Spread (std, px) of staff-line height across the band; 0 = perfectly straight."""
    _, offs = _column_offsets(band)
    return float(np.std(offs)) if offs else 0.0


def _grow(gray: np.ndarray, y: int, direction: int, limit: int) -> int:
    rows_ink = (gray < 128).sum(1)
    blank, moved = 0, 0
    while 0 < y < gray.shape[0] - 1 and moved < limit:
        y += direction
        moved += 1
        blank = blank + 1 if rows_ink[y] == 0 else 0
        if blank >= 6:
            return y
    return y


def snap_to_noteheads(img: np.ndarray, points: list[tuple[float, float]], spacing: float) -> list[tuple[float, float]]:
    """Points come with an exact height (from the pitch) but only a rough x. Move each onto the
    filled notehead at that height: remove staff lines and stems, keep note-sized blobs, pick the
    closest one. Points with no blob there (hollow half/whole notes) keep their position."""
    sp = spacing or TARGET_SPACING
    ink = (img < 140).astype(np.uint8)
    lines = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (int(sp * 2), 1)))
    stems = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, int(sp * 2))))
    heads = ink & ~cv2.dilate(lines, np.ones((3, 1), np.uint8)) & ~stems
    k = max(3, int(sp * 0.55)) | 1
    heads = cv2.morphologyEx(heads, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    n, _, stats, cents = cv2.connectedComponentsWithStats(heads)
    blobs = [(cents[i][0], cents[i][1]) for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= (sp * 0.45) ** 2]
    out = []
    for x, y in points:
        near = [(bx, by) for bx, by in blobs if abs(bx - x) < sp * 1.3 and abs(by - y) < sp * 0.45]
        out.append(min(near, key=lambda b: (b[0] - x) ** 2 + (b[1] - y) ** 2) if near else (x, y))
    return out


# ---------- all together ----------

def keep_faint_ink(gray: np.ndarray, leveled: np.ndarray, spacing: float) -> np.ndarray:
    """Thin staff lines and small text on poor photos can be lighter than the page threshold.
    A local (adaptive) threshold still sees them as darker than their surroundings: make those
    pixels dark so no staff line or symbol disappears."""
    block = max(15, int((spacing or TARGET_SPACING) * 1.6)) | 1
    smooth = cv2.medianBlur(gray, 3)  # so sensor noise isn't mistaken for ink
    local = cv2.adaptiveThreshold(smooth, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, 10)
    out = leveled.copy()
    out[(local > 0) & (out > 70) & (out < 200)] = 70  # only greyish pixels: paper stays white
    return out


def clean(img: np.ndarray) -> tuple[np.ndarray, CleanInfo]:
    info = CleanInfo()
    img, info.page_found = flatten_page(img)
    gray = remove_shadows(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    info.rotation_deg = find_rotation(gray)
    gray = rotate(gray, info.rotation_deg)

    _, spacing = find_staves(gray)
    info.spacing_before = round(spacing, 2)
    if spacing and spacing < TARGET_SPACING * 0.85:
        info.scale = round(min(3.0, TARGET_SPACING / spacing), 2)
    elif not spacing and max(gray.shape) < 2000:  # lines too faint to measure: small image, enlarge first
        info.scale = round(min(3.0, 2400 / max(gray.shape)), 2)
    if info.scale != 1.0:
        gray = cv2.resize(gray, None, fx=info.scale, fy=info.scale, interpolation=cv2.INTER_CUBIC)
        spacing = spacing * info.scale if spacing else TARGET_SPACING
        gray = sharpen(gray, spacing)

    out = keep_faint_ink(gray, levels(gray), spacing)
    out = remove_specks(out, spacing or TARGET_SPACING)

    staves, spacing = find_staves(out)
    info.staves = [[round(a), round(b)] for a, b in staves]
    per = guess_staves_per_system(staves)
    bands = system_bands(staves, out.shape[0], per)
    if bands:  # first and last line: grow until a blank row, so titles, tempo and fingering stay in
        bands[0] = (_grow(out, bands[0][0], -1, int(spacing * 8)), bands[0][1])
        bands[-1] = (bands[-1][0], _grow(out, bands[-1][1], 1, int(spacing * 8)))
    info.systems = [list(b) for b in bands]
    return out, info


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: python cleaning/clean_page.py <image> <out_dir>")
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    page, info = clean(cv2.imread(str(src)))
    cv2.imwrite(str(out / f"{src.stem}_clean.png"), page)
    for i, (y0, y1) in enumerate(info.systems, 1):
        cv2.imwrite(str(out / f"{src.stem}_line{i:02d}.png"), straighten_band(page[y0:y1]))
    (out / f"{src.stem}_clean.json").write_text(json.dumps(asdict(info), indent=1))
    print(json.dumps({k: v for k, v in asdict(info).items() if k not in ("staves",)}, indent=1))


if __name__ == "__main__":
    main()
