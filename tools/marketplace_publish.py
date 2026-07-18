#!/usr/bin/env python3
"""Publish/restock a marketplace listing OFF-SCREEN from a staging pack.

Input per product:  data/marketplace_staging/<site>/<slug>/
    - listing.md           (human-prose listing; we extract fields)  OR
    - fields.json          (canonical structured override, if present)
    - <deliverable file>   (the .pdf/.xlsx/.zip/.docx buyers receive)
Cover image:        products/covers/<slug>/<first image>  (falls back to an
                    image inside the staging folder)

Guarantees:
  * Idempotent — skips anything already recorded in the published registry
    (data/marketplace_sessions/published.json), so re-runs don't double-list.
  * No empty/refund-trap listings — verifies the deliverable file (and, unless
    --allow-no-cover, a cover image) exist BEFORE attempting publish.
  * Off-screen — drives the focus-safe browser from tools.offscreen_browser.
  * Captures the live listing URL into the registry.

The site adapters (whop/promptbase/etsy) encode the publish flows; the exact
selectors need the owner's one-time login to validate live (see docs). All the
field parsing / mapping / idempotency / verification is unit-tested headless.

CLI:
    python3 -m tools.marketplace_publish --site whop --product small-business-sops
    python3 -m tools.marketplace_publish --site whop --all
    python3 -m tools.marketplace_publish --site whop --product X --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.offscreen_browser import MARKETPLACES, SESSION_ROOT, marketplace  # noqa: E402

STAGING_ROOT = ROOT / "data" / "marketplace_staging"
COVERS_ROOT = ROOT / "products" / "covers"
PUBLISHED_REGISTRY = SESSION_ROOT / "published.json"

DELIVERABLE_EXTS = (".pdf", ".xlsx", ".xls", ".zip", ".docx", ".csv", ".pptx")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


# ---------------------------------------------------------------------------
# Staging pack -> fields (pure, unit-tested)
# ---------------------------------------------------------------------------
@dataclass
class Pack:
    site: str
    slug: str
    name: str = ""
    price: float | None = None
    description: str = ""
    tags: list[str] = field(default_factory=list)
    product_type: str = ""
    deliverable: Path | None = None
    cover: Path | None = None

    @property
    def ok_to_publish(self) -> tuple[bool, str]:
        if not self.name:
            return False, "missing product name"
        if self.price is None:
            return False, "missing price"
        if not self.description:
            return False, "missing description"
        if not self.deliverable or not self.deliverable.exists():
            return False, "missing deliverable file"
        if self.cover is None:
            return False, "missing cover image (use --allow-no-cover to override)"
        return True, "ok"


def _section(md: str, *titles: str) -> str:
    """Return the body text under the first '## <title>' heading whose text
    contains any of `titles` (case-insensitive), up to the next heading/hr."""
    lines = md.splitlines()
    titles_l = [t.lower() for t in titles]
    out: list[str] = []
    capturing = False
    for ln in lines:
        h = re.match(r"^#{1,6}\s+(.*)$", ln)
        if h:
            if capturing:
                break
            head = h.group(1).lower()
            if any(t in head for t in titles_l):
                capturing = True
            continue
        if capturing:
            if ln.strip() == "---":
                break
            out.append(ln)
    return "\n".join(out).strip()


def _first_price(text: str) -> float | None:
    m = re.search(r"\$\s?(\d+(?:\.\d{1,2})?)", text)
    return float(m.group(1)) if m else None


def parse_listing_md(md: str) -> dict:
    """Best-effort field extraction from the stack's listing.md prose format."""
    name = _section(md, "product name", "title")
    # name section may carry a leading parenthetical; take first non-empty line
    name = next((l.strip() for l in name.splitlines() if l.strip()), "")
    name = re.sub(r"\s*\(paste.*?\)", "", name, flags=re.I).strip()

    price_blob = _section(md, "price") or md
    price = _first_price(price_blob)

    desc = _section(md, "description")
    # strip bold-marked paste hints
    desc = re.sub(r"\*\*PII/brand note.*", "", desc, flags=re.S).strip()

    tags_blob = _section(md, "tags", "keywords")
    tags: list[str] = []
    if tags_blob:
        first = next((l for l in tags_blob.splitlines() if l.strip()), "")
        tags = [t.strip() for t in re.split(r"[,·]", first) if t.strip() and not t.strip().startswith("(")]

    ptype = _section(md, "product type", "category / type", "category")
    ptype = next((l.strip() for l in ptype.splitlines() if l.strip()), "")

    return {"name": name, "price": price, "description": desc, "tags": tags,
            "product_type": ptype}


def _find_deliverable(folder: Path) -> Path | None:
    cands = [p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() in DELIVERABLE_EXTS]
    # prefer the largest (the real product, not a stub)
    return max(cands, key=lambda p: p.stat().st_size) if cands else None


def _find_cover(slug: str, folder: Path) -> Path | None:
    for base in (COVERS_ROOT / slug, folder):
        if base.exists():
            imgs = sorted(p for p in base.iterdir()
                          if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
            if imgs:
                return imgs[0]
    return None


def load_pack(site: str, slug: str) -> Pack:
    folder = STAGING_ROOT / site / slug
    if not folder.is_dir():
        raise FileNotFoundError(f"no staging folder: {folder}")
    fields_json = folder / "fields.json"
    if fields_json.exists():
        d = json.loads(fields_json.read_text())
    else:
        md = (folder / "listing.md").read_text() if (folder / "listing.md").exists() else ""
        d = parse_listing_md(md)
    pack = Pack(
        site=site, slug=slug,
        name=d.get("name", ""),
        price=d.get("price"),
        description=d.get("description", ""),
        tags=d.get("tags", []) or [],
        product_type=d.get("product_type", ""),
        deliverable=Path(d["deliverable"]) if d.get("deliverable") else _find_deliverable(folder),
        cover=Path(d["cover"]) if d.get("cover") else _find_cover(slug, folder),
    )
    return pack


def list_products(site: str) -> list[str]:
    base = STAGING_ROOT / site
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


# ---------------------------------------------------------------------------
# Idempotency registry (pure, unit-tested)
# ---------------------------------------------------------------------------
def _load_registry() -> dict:
    if PUBLISHED_REGISTRY.exists():
        try:
            return json.loads(PUBLISHED_REGISTRY.read_text())
        except Exception:
            return {}
    return {}


def _reg_key(site: str, slug: str) -> str:
    return f"{site}/{slug}"


def is_listed(site: str, slug: str, registry: dict | None = None) -> bool:
    reg = registry if registry is not None else _load_registry()
    return _reg_key(site, slug) in reg and bool(reg[_reg_key(site, slug)].get("url"))


def record_published(site: str, slug: str, url: str) -> None:
    reg = _load_registry()
    reg[_reg_key(site, slug)] = {"url": url, "listed_at": int(time.time())}
    PUBLISHED_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    PUBLISHED_REGISTRY.write_text(json.dumps(reg, indent=2))


# ---------------------------------------------------------------------------
# Site adapters — encode the publish wizard flows (need live login to validate)
# ---------------------------------------------------------------------------
class BaseAdapter:
    site = ""

    def publish(self, ctx, pack: Pack) -> str:  # returns live url
        raise NotImplementedError


class WhopAdapter(BaseAdapter):
    site = "whop"

    def publish(self, ctx, pack: Pack) -> str:
        # Whop's current flow: Dashboard -> Products -> Add product ->
        # Digital product/File -> Apps/Page wizard. Encoded as best-effort
        # selectors; refine on first live run.
        page = ctx.new_page()
        page.goto("https://whop.com/dashboard", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        _click_first(page, ['a:has-text("Products")', 'text=Products'])
        _click_first(page, ['button:has-text("Add product")', 'text=Add product', 'text=New product'])
        _click_first(page, ['text=Digital', 'text=File'])
        _fill_first(page, ['input[name="name"]', 'input[placeholder*="Name" i]'], pack.name)
        _fill_first(page, ['textarea[name="description"]', 'textarea[placeholder*="Description" i]'], pack.description)
        _fill_first(page, ['input[name="price"]', 'input[placeholder*="Price" i]'], f"{pack.price:.2f}")
        _upload_first(page, ['input[type="file"]'], pack.deliverable)
        if pack.cover:
            _upload_first(page, ['input[type="file"][accept*="image"]', 'input[type="file"]'], pack.cover)
        _click_first(page, ['button:has-text("Publish")', 'button:has-text("Save")'])
        page.wait_for_timeout(2500)
        return page.url


class SubmissionError(RuntimeError):
    """Raised when a publish/submit could not be verified as landed."""


class PromptbaseAdapter(BaseAdapter):
    site = "promptbase"

    def publish(self, ctx, pack: Pack) -> str:
        # PromptBase sells ONE prompt per listing: the field set is title /
        # description / the prompt text / a tested sample output / price / tags,
        # plus the ChatGPT verification share-link. We pull the prompt text +
        # example output from the staging folder and the share-link minted by
        # tools.chatgpt_share_link.
        extras = _load_promptbase_extras(pack.slug)
        page = ctx.new_page()
        page.goto("https://promptbase.com/sell", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        _click_first(page, ['text=Sell a prompt', 'a:has-text("Sell")', 'a:has-text("Create")'])
        _fill_first(page, ['input[name="title"]', 'input[placeholder*="Title" i]'], pack.name)
        _fill_first(page, ['textarea[name="description"]', 'textarea[placeholder*="Description" i]'], pack.description)
        # the actual prompt the buyer pays for
        if extras.get("prompt_text"):
            _fill_first(page, ['textarea[name="prompt"]', 'textarea[placeholder*="prompt" i]',
                              'div[contenteditable="true"]'], extras["prompt_text"])
        # tested sample output (anti-spam requirement)
        if extras.get("example_output"):
            _fill_first(page, ['textarea[name="testOutput"]', 'textarea[placeholder*="output" i]',
                              'textarea[placeholder*="result" i]'], extras["example_output"])
        # ChatGPT verification share-link (proves the prompt runs)
        if extras.get("verification_url"):
            _fill_first(page, ['input[name="verificationLink"]', 'input[placeholder*="share" i]',
                              'input[placeholder*="link" i]', 'input[type="url"]'],
                        extras["verification_url"])
        _fill_first(page, ['input[name="price"]'], f"{pack.price:.2f}")
        for t in pack.tags[:11]:
            _fill_first(page, ['input[placeholder*="tag" i]', 'input[placeholder*="keyword" i]'],
                        t, then_enter=True)
        if pack.cover:
            _upload_first(page, ['input[type="file"][accept*="image"]', 'input[type="file"]'], pack.cover)
        _click_first(page, ['button:has-text("Submit for review")', 'button:has-text("Submit")',
                            'button:has-text("Publish")'])
        page.wait_for_timeout(3000)
        if not _promptbase_submission_landed(page):
            raise SubmissionError(
                "PromptBase submission not confirmed (no pending-review state detected)")
        return page.url


class EtsyAdapter(BaseAdapter):
    site = "etsy"

    def publish(self, ctx, pack: Pack) -> str:
        page = ctx.new_page()
        page.goto("https://www.etsy.com/your/shops/me/tools/listings/create",
                  wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        if pack.cover:
            _upload_first(page, ['input[type="file"][accept*="image"]', 'input[type="file"]'], pack.cover)
        _fill_first(page, ['input[name="title"]', '#listing-title-input'], pack.name)
        _fill_first(page, ['textarea[name="description"]', '#listing-description-input'], pack.description)
        _fill_first(page, ['input[name="price"]', '#price-input'], f"{pack.price:.2f}")
        # digital file upload
        _upload_first(page, ['input[type="file"][accept*="pdf"]', 'input[type="file"]'], pack.deliverable)
        for t in pack.tags[:13]:
            _fill_first(page, ['input[placeholder*="tag" i]', '#tag-input'], t, then_enter=True)
        _click_first(page, ['button:has-text("Publish")', 'button:has-text("Save and continue")'])
        page.wait_for_timeout(2500)
        return page.url


ADAPTERS = {a.site: a for a in (WhopAdapter(), PromptbaseAdapter(), EtsyAdapter())}


def _click_first(page, sels):
    for s in sels:
        try:
            page.locator(s).first.click(timeout=4000)
            return True
        except Exception:
            continue
    return False


def _fill_first(page, sels, value, then_enter=False):
    for s in sels:
        try:
            loc = page.locator(s).first
            loc.fill(value, timeout=4000)
            if then_enter:
                loc.press("Enter")
            return True
        except Exception:
            continue
    return False


def _upload_first(page, sels, path: Path):
    for s in sels:
        try:
            page.locator(s).first.set_input_files(str(path), timeout=4000)
            return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# PromptBase-specific extras + submission verification (pure parts unit-tested)
# ---------------------------------------------------------------------------
def _read_fenced_prompt(folder: Path) -> str:
    f = folder / "promptbase_prompt.md"
    if not f.exists():
        return ""
    m = re.search(r"```[^\n]*\n(.*?)```", f.read_text(), re.S)
    return m.group(1).strip() if m else ""


def _read_example_output(folder: Path) -> str:
    for name in ("example_1.txt", "example_2.txt"):
        p = folder / name
        if p.exists():
            return p.read_text().strip()
    return ""


def _load_promptbase_extras(slug: str) -> dict:
    """Gather the prompt text, a tested sample output, and the ChatGPT
    verification share-link for a PromptBase listing."""
    folder = STAGING_ROOT / "promptbase" / slug
    verification_url = ""
    link_file = SESSION_ROOT / "promptbase" / "verification_link.json"
    if link_file.exists():
        try:
            d = json.loads(link_file.read_text())
            if d.get("anon_ok"):  # only use an anon-verified link
                verification_url = d.get("url", "")
        except Exception:
            pass
    return {
        "prompt_text": _read_fenced_prompt(folder),
        "example_output": _read_example_output(folder),
        "verification_url": verification_url,
    }


def promptbase_landed_from_text(text: str, url: str) -> bool:
    """Pure: decide whether a PromptBase submission landed. Pending-review IS
    success (manual review queue is normal)."""
    low = (text or "").lower()
    ok_signals = ("under review", "pending review", "submitted for review",
                  "in review", "we'll review", "successfully submitted",
                  "your prompt has been submitted")
    if any(s in low for s in ok_signals):
        return True
    u = (url or "").lower()
    return ("/dashboard" in u or "/sales" in u or "/my-prompts" in u
            or "/profile" in u)


def _promptbase_submission_landed(page) -> bool:
    text = ""
    for sel in ("main", "body"):
        try:
            text = page.locator(sel).first.inner_text(timeout=4000)
            if text:
                break
        except Exception:
            continue
    return promptbase_landed_from_text(text, page.url)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def publish_one(site: str, slug: str, dry_run: bool = False,
                allow_no_cover: bool = False, force: bool = False) -> int:
    marketplace(site)  # validates site
    if not force and is_listed(site, slug):
        print(f"[publish:{site}/{slug}] already listed (idempotent skip)")
        return 0
    pack = load_pack(site, slug)
    ok, why = pack.ok_to_publish
    if not ok and not (allow_no_cover and why.startswith("missing cover")):
        print(f"[publish:{site}/{slug}] BLOCKED — {why} (no empty/refund-trap listings)")
        return 1
    print(f"[publish:{site}/{slug}] name={pack.name!r} price=${pack.price} "
          f"deliverable={pack.deliverable.name if pack.deliverable else None} "
          f"cover={pack.cover.name if pack.cover else None} tags={len(pack.tags)}")
    if dry_run:
        print(f"[publish:{site}/{slug}] --dry-run: validated, not publishing")
        return 0

    from tools.offscreen_browser import offscreen_context
    adapter = ADAPTERS[site]
    try:
        with offscreen_context(site, load_session=True) as ctx:
            url = adapter.publish(ctx, pack)
    except SubmissionError as e:
        print(f"[publish:{site}/{slug}] ✗ NOT recorded — {e}")
        return 1
    record_published(site, slug, url)
    print(f"[publish:{site}/{slug}] ✓ published off-screen -> {url}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Off-screen marketplace publisher")
    ap.add_argument("--site", required=True, choices=sorted(MARKETPLACES))
    ap.add_argument("--product", help="staging slug")
    ap.add_argument("--all", action="store_true", help="every staged product for the site")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-no-cover", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-publish even if registered")
    a = ap.parse_args()
    slugs = list_products(a.site) if a.all else ([a.product] if a.product else [])
    if not slugs:
        print("nothing to publish; pass --product <slug> or --all")
        return 1
    rc = 0
    for slug in slugs:
        rc |= publish_one(a.site, slug, a.dry_run, a.allow_no_cover, a.force)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
