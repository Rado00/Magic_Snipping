"""Scrape a deckstats.net deck, download every card image at the highest
available resolution, and compose a printable PDF.

The deck page embeds the full decklist as a JS call ``deck_data({...})``;
we extract that JSON and look each card up via the Scryfall API
(image_uris.png ~745x1040 — the highest resolution publicly available).

Usage:
    python magic_snipping.py [--input deck_link.txt] [--output output/]
                             [--dedupe] [--cols 3] [--rows 3]
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import requests
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

SCRYFALL = "https://api.scryfall.com"

LOG = logging.getLogger("magic_snipping")


@dataclass(frozen=True)
class Card:
    name: str
    quantity: int = 1
    collector_number: str = ""
    set_id: int | None = None  # deckstats internal id, not a Scryfall set code

    @property
    def slug(self) -> str:
        safe_name = re.sub(r"[^A-Za-z0-9]+", "_", self.name).strip("_")
        suffix = f"_{self.collector_number}" if self.collector_number else ""
        return f"{safe_name}{suffix}"


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


_DECK_DATA_RE = re.compile(r"deck_data\((\{.+?\})\)\s*;", re.DOTALL)


def _extract_deck_json(html: str) -> dict | None:
    """Find the deck_data({...}) blob and return the parsed JSON.

    The JSON ends at a balanced closing brace; deck_data may appear multiple
    times in the page so we balance braces manually instead of using a lazy
    regex (which would break on nested objects).
    """
    idx = html.find("deck_data(")
    if idx < 0:
        return None
    start = html.find("{", idx)
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(html)):
        c = html[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                blob = html[start : i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError as e:
                    LOG.error("Failed to parse deck JSON: %s", e)
                    return None
    return None


SKIP_SECTIONS = {"sideboard", "maybeboard", "tokens"}


def scrape_deck(deck_url: str, *, include_sideboard: bool = False) -> list[Card]:
    LOG.info("Fetching deck page: %s", deck_url)
    html = fetch(deck_url).text

    data = _extract_deck_json(html)
    if not data:
        debug_path = Path("output") / "debug_deck.html"
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(html, encoding="utf-8")
        LOG.error("Could not find deck_data(...) JSON. Saved page to %s", debug_path)
        return []

    cards: list[Card] = []
    for section in data.get("sections", []):
        sname = (section.get("name") or "").strip().lower()
        if not include_sideboard and sname in SKIP_SECTIONS:
            LOG.info("Skipping section %r", section.get("name"))
            continue
        LOG.info("Section %r: %d entries", section.get("name"), len(section.get("cards", [])))
        for entry in section.get("cards", []):
            name = entry.get("name") or ""
            if not name:
                continue
            qty = int(entry.get("amount", 1) or 1)
            cn = (
                entry.get("collector_number")
                or entry.get("data", {}).get("collector_number")
                or ""
            )
            cards.append(Card(
                name=name,
                quantity=qty,
                collector_number=str(cn),
                set_id=entry.get("set_id"),
            ))

    cards.sort(key=lambda c: c.name)
    LOG.info("Parsed %d unique cards (%d total copies)",
             len(cards), sum(c.quantity for c in cards))
    return cards


# Scryfall is rate-limited at ~10 req/s — we add a small delay between calls.
_SCRYFALL_DELAY = 0.1


def _scryfall_named(name: str) -> dict | None:
    url = f"{SCRYFALL}/cards/named?exact={quote(name)}"
    try:
        r = SESSION.get(url, timeout=30)
        if r.status_code == 404:
            # Try fuzzy as a last resort (handles minor typos / DFC name variants)
            url = f"{SCRYFALL}/cards/named?fuzzy={quote(name)}"
            r = SESSION.get(url, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        LOG.error("Scryfall lookup failed for %r: %s", name, e)
        return None
    finally:
        time.sleep(_SCRYFALL_DELAY)


def _image_urls_from_scryfall(card_json: dict, *, include_backs: bool = False) -> list[tuple[str, str]]:
    """Return [(face_label, png_url), ...]. For DFC/MDFC cards, the back face is
    only included when include_backs is True (default: front only, so the image
    count matches the decklist count)."""
    out: list[tuple[str, str]] = []
    if "image_uris" in card_json and "png" in card_json["image_uris"]:
        out.append(("", card_json["image_uris"]["png"]))
        return out
    faces = card_json.get("card_faces") or []
    for i, face in enumerate(faces):
        if i > 0 and not include_backs:
            break
        uris = face.get("image_uris") or {}
        if "png" in uris:
            out.append((f"face{i + 1}", uris["png"]))
    return out


def download_images(cards: Iterable[Card], images_dir: Path, *, include_backs: bool = False) -> dict[Card, list[Path]]:
    images_dir.mkdir(parents=True, exist_ok=True)
    saved: dict[Card, list[Path]] = {}
    cards = list(cards)
    for i, card in enumerate(cards, 1):
        # If we already have at least one image cached for this slug, reuse it.
        # When include_backs is False, prefer files without a face2 suffix.
        existing = sorted(images_dir.glob(f"{card.slug}*.png"))
        if not include_backs:
            existing = [p for p in existing if "_face2" not in p.stem]
        if existing:
            LOG.info("[%d/%d] cached %s (%d file(s))", i, len(cards), card.slug, len(existing))
            saved[card] = existing
            continue

        LOG.info("[%d/%d] %s", i, len(cards), card.name)
        meta = _scryfall_named(card.name)
        if not meta:
            continue
        urls = _image_urls_from_scryfall(meta, include_backs=include_backs)
        if not urls:
            LOG.warning("  no image_uris.png for %r", card.name)
            continue

        paths: list[Path] = []
        for label, url in urls:
            target = images_dir / f"{card.slug}{('_' + label) if label else ''}.png"
            try:
                LOG.info("  -> %s", url)
                data = fetch(url).content
                with Image.open(io.BytesIO(data)) as im:
                    im.load()
                    im.convert("RGB").save(target, format="PNG")
                paths.append(target)
            except Exception as e:
                LOG.error("  failed to save %s: %s", target.name, e)
        if paths:
            saved[card] = paths
        time.sleep(0.2)
    return saved


def build_pdf(
    cards: list[Card],
    images: dict[Card, list[Path]],
    output_pdf: Path,
    *,
    card_w_mm: float = 63.0,
    card_h_mm: float = 88.0,
    expand_quantity: bool = True,
    gutter_mm: float = 0.0,
    margin_mm: float = 5.0,
) -> None:
    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    sequence: list[Path] = []
    for card in cards:
        paths = images.get(card)
        if not paths:
            continue
        copies = card.quantity if expand_quantity else 1
        for _ in range(copies):
            sequence.extend(paths)  # both faces of a DFC printed back-to-back

    if not sequence:
        LOG.error("No images to put in the PDF — aborting.")
        return

    page_w, page_h = A4
    margin = margin_mm * mm
    gutter = gutter_mm * mm
    cell_w = card_w_mm * mm
    cell_h = card_h_mm * mm

    cols = max(1, int((page_w - 2 * margin + gutter) // (cell_w + gutter)))
    rows = max(1, int((page_h - 2 * margin + gutter) // (cell_h + gutter)))
    per_page = cols * rows

    # Center the grid on the page so trim marks align with what you measure.
    grid_w = cols * cell_w + (cols - 1) * gutter
    grid_h = rows * cell_h + (rows - 1) * gutter
    x_offset = (page_w - grid_w) / 2
    y_offset = (page_h - grid_h) / 2

    LOG.info(
        "PDF: %d images, %dx%d per page, cell %.2fx%.2fmm, gutter %.1fmm",
        len(sequence), cols, rows, card_w_mm, card_h_mm, gutter_mm,
    )

    c = canvas.Canvas(str(output_pdf), pagesize=A4)

    for i, img_path in enumerate(sequence):
        slot = i % per_page
        if i > 0 and slot == 0:
            c.showPage()
        col = slot % cols
        row = slot // cols
        x = x_offset + col * (cell_w + gutter)
        y = page_h - y_offset - (row + 1) * cell_h - row * gutter
        try:
            # preserveAspectRatio=False so the image fills the exact cell —
            # this is what makes printer compensation actually work.
            c.drawImage(str(img_path), x, y, width=cell_w, height=cell_h,
                        preserveAspectRatio=False, mask="auto")
        except Exception as e:
            LOG.error("Could not place %s: %s", img_path, e)

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
    parser.add_argument("--include-sideboard", action="store_true",
                        help="Also include sideboard / maybeboard / tokens sections.")
    parser.add_argument("--no-backs", action="store_true",
                        help="For double-faced cards, only save the front face. "
                             "Default: both faces are included.")
    parser.add_argument("--card-width", type=float, default=63.0,
                        help="Target printed card width in mm (MTG official: 63).")
    parser.add_argument("--card-height", type=float, default=88.0,
                        help="Target printed card height in mm (MTG official: 88).")
    parser.add_argument("--measured-width", type=float, default=None,
                        help="If you printed a test page and the cards came out narrower than "
                             "--card-width, pass the measured width here. The PDF will be "
                             "pre-scaled to compensate for your printer.")
    parser.add_argument("--measured-height", type=float, default=None,
                        help="Same as --measured-width but for height.")
    parser.add_argument("--gutter", type=float, default=0.0,
                        help="Spacing in mm between cards (default 0: cards adjacent so a "
                             "single straight cut separates two of them).")
    parser.add_argument("--margin", type=float, default=5.0,
                        help="Outer page margin in mm (default 5).")
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

    cards = scrape_deck(deck_url, include_sideboard=args.include_sideboard)
    if not cards:
        return 2

    images = download_images(cards, images_dir, include_backs=not args.no_backs)
    if not images:
        LOG.error("No images were downloaded — see warnings above.")
        return 3

    # Printer compensation: if the user measured smaller-than-target prints,
    # scale up the PDF cell so the printed result matches the target size.
    pdf_card_w = args.card_width
    pdf_card_h = args.card_height
    if args.measured_width and args.measured_width > 0:
        pdf_card_w = args.card_width * (args.card_width / args.measured_width)
        LOG.info("Compensating width: measured %.2f → PDF cell %.2f mm",
                 args.measured_width, pdf_card_w)
    if args.measured_height and args.measured_height > 0:
        pdf_card_h = args.card_height * (args.card_height / args.measured_height)
        LOG.info("Compensating height: measured %.2f → PDF cell %.2f mm",
                 args.measured_height, pdf_card_h)

    build_pdf(
        cards,
        images,
        pdf_path,
        card_w_mm=pdf_card_w,
        card_h_mm=pdf_card_h,
        expand_quantity=not args.dedupe,
        gutter_mm=args.gutter,
        margin_mm=args.margin,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
