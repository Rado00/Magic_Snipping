"""Scrape a deckstats.net deck, download every card image at the highest
available resolution, and compose a printable PDF.

Usage:
    python magic_snipping.py [--input deck_link.txt] [--output output/]
                             [--dedupe] [--per-page 9]
"""

from __future__ import annotations

import argparse
import io
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})

LOG = logging.getLogger("magic_snipping")


@dataclass(frozen=True)
class Card:
    name: str
    set_code: str
    collector_number: str
    quantity: int = 1

    @property
    def slug(self) -> str:
        safe_name = re.sub(r"[^A-Za-z0-9]+", "_", self.name).strip("_")
        return f"{safe_name}__{self.set_code}_{self.collector_number}"

    @property
    def card_page_url(self) -> str:
        from urllib.parse import urlencode

        params = {
            "utf8": "1",
            "lng": "en",
            "card": self.name,
            "set": self.set_code,
            "collector_number": self.collector_number,
        }
        return "https://cards.deckstats.net/magiccard.php?" + urlencode(params)


def fetch(url: str, *, retries: int = 3, backoff: float = 2.0) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = SESSION.get(url, timeout=30)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_exc = e
            LOG.warning("GET failed (%s/%s) %s -> %s", attempt, retries, url, e)
            if attempt < retries:
                time.sleep(backoff ** attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_exc}")


def parse_card_link(href: str) -> Card | None:
    """Turn an href like //cards.deckstats.net/magiccard.php?... into a Card."""
    if "magiccard.php" not in href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    name = qs.get("card", [""])[0]
    set_code = qs.get("set", [""])[0]
    cn = qs.get("collector_number", [""])[0]
    if not name or not set_code or not cn:
        return None
    return Card(name=unquote(name), set_code=set_code, collector_number=cn)


def scrape_deck(deck_url: str) -> list[Card]:
    LOG.info("Fetching deck page: %s", deck_url)
    html = fetch(deck_url).text
    soup = BeautifulSoup(html, "lxml")

    counts: dict[Card, int] = {}
    for a in soup.find_all("a", href=True):
        card = parse_card_link(a["href"])
        if card is None:
            continue
        # Try to read quantity from a sibling/parent (deckstats lists "1x Card name")
        qty = _extract_quantity_near(a)
        counts[card] = counts.get(card, 0) + qty

    if not counts:
        LOG.error(
            "No cards found on deck page. The page may be JS-rendered or the "
            "selector is outdated. Saving HTML to output/debug_deck.html for "
            "inspection."
        )
        debug_path = Path("output") / "debug_deck.html"
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(html, encoding="utf-8")

    cards = [Card(c.name, c.set_code, c.collector_number, q) for c, q in counts.items()]
    cards.sort(key=lambda c: (c.set_code, c.collector_number, c.name))
    LOG.info("Found %d unique cards (%d total)", len(cards), sum(c.quantity for c in cards))
    return cards


def _extract_quantity_near(anchor) -> int:
    """Look for a number like '1x' or '2 ' near the anchor; default to 1."""
    for node in (anchor.parent, anchor.previous_sibling, anchor.find_previous()):
        if node is None:
            continue
        text = getattr(node, "text", str(node))
        m = re.search(r"(\d+)\s*[x×]?", text)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 99:
                return n
    return 1


IMAGE_SELECTORS = [
    {"id": "card_image"},
    {"class_": "card_image_full"},
    {"class_": "mtg_card"},
    {"class_": "card_picture"},
]


def extract_card_image_url(card_page_html: str) -> str | None:
    soup = BeautifulSoup(card_page_html, "lxml")
    for sel in IMAGE_SELECTORS:
        img = soup.find("img", **sel)
        if img and img.get("src"):
            return _abs_url(img["src"])

    # Fallback: pick the <img> whose src looks like a card art file
    candidates = []
    for img in soup.find_all("img", src=True):
        src = img["src"].lower()
        if any(token in src for token in ("scryfall", "card", "magic", ".png", ".jpg")):
            candidates.append(img["src"])
    if candidates:
        # Prefer scryfall PNG if present (highest res)
        for c in candidates:
            if "scryfall" in c.lower() and c.lower().endswith(".png"):
                return _abs_url(c)
        return _abs_url(candidates[0])
    return None


def _abs_url(src: str) -> str:
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return "https://cards.deckstats.net" + src
    return src


def _upgrade_to_png(url: str) -> str:
    """Scryfall serves the same image at multiple sizes; PNG is highest-res."""
    if "scryfall" not in url:
        return url
    # /normal/, /large/, /small/ -> /png/   ; .jpg -> .png
    upgraded = re.sub(r"/(small|normal|large|border_crop|art_crop)/", "/png/", url)
    upgraded = re.sub(r"\.jpg(\?|$)", r".png\1", upgraded)
    return upgraded


def download_images(cards: Iterable[Card], images_dir: Path) -> dict[Card, Path]:
    images_dir.mkdir(parents=True, exist_ok=True)
    saved: dict[Card, Path] = {}
    cards = list(cards)
    for i, card in enumerate(cards, 1):
        # Use .png by default; if downloader returns jpg, suffix is fixed afterwards
        target = images_dir / f"{card.slug}.png"
        if target.exists() and target.stat().st_size > 0:
            LOG.info("[%d/%d] cached %s", i, len(cards), card.slug)
            saved[card] = target
            continue

        LOG.info("[%d/%d] %s (%s #%s)", i, len(cards), card.name, card.set_code, card.collector_number)
        try:
            page_html = fetch(card.card_page_url).text
            img_url = extract_card_image_url(page_html)
            if not img_url:
                LOG.warning("  no image found on card page")
                continue
            img_url = _upgrade_to_png(img_url)
            LOG.info("  -> %s", img_url)
            img_bytes = fetch(img_url).content
            # Normalize to PNG via Pillow (also validates the file)
            with Image.open(io.BytesIO(img_bytes)) as im:
                im.load()
                im.convert("RGB").save(target, format="PNG")
            saved[card] = target
        except Exception as e:
            LOG.error("  failed: %s", e)
        time.sleep(0.4)  # be polite
    return saved


def build_pdf(
    cards: list[Card],
    images: dict[Card, Path],
    output_pdf: Path,
    *,
    cols: int = 3,
    rows: int = 3,
    expand_quantity: bool = True,
    separator: bool = True,
) -> None:
    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    # Build the print sequence (respecting quantities if requested)
    sequence: list[Path] = []
    for card in cards:
        path = images.get(card)
        if path is None:
            continue
        copies = card.quantity if expand_quantity else 1
        sequence.extend([path] * copies)

    if not sequence:
        LOG.error("No images to put in the PDF — aborting.")
        return

    page_w, page_h = A4
    # Standard MTG card aspect = 63 x 88 mm, leave margins
    margin = 8 * mm
    gutter = 2 * mm
    cell_w = (page_w - 2 * margin - (cols - 1) * gutter) / cols
    cell_h = (page_h - 2 * margin - (rows - 1) * gutter) / rows
    # Force MTG aspect (88/63 ≈ 1.397) to avoid stretching
    aspect = 88.0 / 63.0
    if cell_h / cell_w > aspect:
        cell_h = cell_w * aspect
    else:
        cell_w = cell_h / aspect

    LOG.info("PDF: %d images, %dx%d per page, cell %.1fx%.1fmm",
             len(sequence), cols, rows, cell_w / mm, cell_h / mm)

    c = canvas.Canvas(str(output_pdf), pagesize=A4)
    per_page = cols * rows

    for i, img_path in enumerate(sequence):
        slot = i % per_page
        if i > 0 and slot == 0:
            c.showPage()
        col = slot % cols
        row = slot // cols
        x = margin + col * (cell_w + gutter)
        # Reportlab origin is bottom-left
        y = page_h - margin - (row + 1) * cell_h - row * gutter
        try:
            c.drawImage(str(img_path), x, y, width=cell_w, height=cell_h,
                        preserveAspectRatio=True, anchor="c", mask="auto")
        except Exception as e:
            LOG.error("Could not place %s: %s", img_path, e)

        if separator:
            c.setStrokeColorRGB(0.6, 0.6, 0.6)
            c.setLineWidth(0.3)
            # vertical line on the right of each cell except last column
            if col < cols - 1:
                lx = x + cell_w + gutter / 2
                c.line(lx, y, lx, y + cell_h)
            # horizontal line below each cell except last row
            if row < rows - 1:
                ly = y - gutter / 2
                c.line(x, ly, x + cell_w, ly)

    c.save()
    LOG.info("Wrote %s (%d pages)", output_pdf,
             (len(sequence) + per_page - 1) // per_page)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="deck_link.txt",
                        help="Text file containing the deck URL on the first line.")
    parser.add_argument("--output", default="output",
                        help="Directory for downloaded images and the final PDF.")
    parser.add_argument("--pdf", default=None,
                        help="Output PDF path (default: <output>/deck.pdf).")
    parser.add_argument("--dedupe", action="store_true",
                        help="Print one copy per unique card instead of N copies.")
    parser.add_argument("--cols", type=int, default=3)
    parser.add_argument("--rows", type=int, default=3)
    parser.add_argument("--no-separator", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    input_path = Path(args.input)
    if not input_path.exists():
        LOG.error("Input file not found: %s", input_path)
        return 1
    deck_url = input_path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    if not deck_url:
        LOG.error("Empty deck URL in %s", input_path)
        return 1

    output_dir = Path(args.output)
    images_dir = output_dir / "images"
    pdf_path = Path(args.pdf) if args.pdf else output_dir / "deck.pdf"

    cards = scrape_deck(deck_url)
    if not cards:
        return 2

    images = download_images(cards, images_dir)
    if not images:
        LOG.error("No images were downloaded — see warnings above.")
        return 3

    build_pdf(
        cards,
        images,
        pdf_path,
        cols=args.cols,
        rows=args.rows,
        expand_quantity=not args.dedupe,
        separator=not args.no_separator,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
