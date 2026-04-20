#!/usr/bin/env python
"""Build the GitHub Pages site under docs/.

Scans data/ to produce:
  - docs/manifest.json    — full file index (for JS search + external tools)
  - docs/stats.json       — aggregate counts
  - docs/sitemap.xml      — for crawlers
  - docs/browse/YYYY/index.html  — per-year static file listing (SEO-friendly)
  - docs/og.png           — 1200x630 Open Graph card, generated via Pillow

Substitutes {{SITE_URL}} and {{REPO_URL}} placeholders in every docs/**/*.html
and in llms.txt / robots.txt.

Config comes from docs/_config.json (you commit this once with your URLs).

Idempotent: safe to run repeatedly. Output files are rewritten atomically.

Usage:
    uv run python scripts/build_docs.py
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from nse_universe.core.db import db, rebuild_from_parquet
from nse_universe.core.export import import_all_if_missing
from nse_universe.paths import ACTIONS_DIR, DATA_DIR, PARQUET_DIR, RAW_DIR, REPO_ROOT

DOCS = REPO_ROOT / "docs"
CONFIG_PATH = DOCS / "_config.json"
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


@dataclass
class SiteConfig:
    site_url: str    # e.g. "https://javaindia.github.io/nse500"
    repo_url: str    # e.g. "https://github.com/javaindia/nse500"
    title: str = "nse-universe"

    @classmethod
    def load(cls) -> "SiteConfig":
        if not CONFIG_PATH.exists():
            raise SystemExit(
                f"missing {CONFIG_PATH}. Create it with:\n"
                '  {"site_url": "https://<user>.github.io/nse500", '
                '"repo_url": "https://github.com/<user>/nse500"}'
            )
        with CONFIG_PATH.open() as f:
            raw = json.load(f)
        su = raw["site_url"].rstrip("/")
        ru = raw["repo_url"].rstrip("/")
        return cls(site_url=su, repo_url=ru, title=raw.get("title", "nse-universe"))


# ───────────────────── manifest + stats ──────────────────────────────

def _date_from_zip(path: Path) -> date | None:
    name = path.name
    # legacy: cmDDMMMYYYYbhav.csv.zip
    m = re.match(r"cm(\d{2})([A-Z]{3})(\d{4})bhav\.csv\.zip$", name)
    if m:
        dd, mmm, yyyy = m.group(1), m.group(2), m.group(3)
        month = MONTHS.index(mmm) + 1
        return date(int(yyyy), month, int(dd))
    # new: BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
    m = re.match(r"BhavCopy_NSE_CM_0_0_0_(\d{8})_F_0000\.csv\.zip$", name)
    if m:
        ymd = m.group(1)
        return date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
    return None


def build_manifest(cfg: SiteConfig) -> dict:
    """Walk data/ and emit a manifest grouped by year."""
    years: dict[str, dict] = {}

    # index parquet by date for quick lookup
    parquet_by_date: dict[date, Path] = {}
    for p in PARQUET_DIR.glob("year=*/month=*/*.parquet"):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue
        parquet_by_date[d] = p

    for z in sorted(RAW_DIR.glob("*/*/*.zip")):
        d = _date_from_zip(z)
        if d is None:
            continue
        yr = str(d.year)
        years.setdefault(yr, {"days": [], "count": 0})
        pq = parquet_by_date.get(d)
        entry = {
            "date": d.isoformat(),
            "zip": str(z.relative_to(REPO_ROOT)),
            "zip_bytes": z.stat().st_size,
        }
        if pq is not None and pq.exists():
            entry["parquet"] = str(pq.relative_to(REPO_ROOT))
            entry["parquet_bytes"] = pq.stat().st_size
        years[yr]["days"].append(entry)

    for yr, info in years.items():
        info["days"].sort(key=lambda e: e["date"])
        info["count"] = len(info["days"])

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "site_url": cfg.site_url,
        "years": years,
    }
    return manifest


def build_stats(cfg: SiteConfig, manifest: dict) -> dict:
    """Aggregate counts, pulling live from DuckDB for symbol/row stats."""
    stats: dict = {
        "generated_at": manifest["generated_at"],
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_files_raw": sum(y["count"] for y in manifest["years"].values()),
    }
    try:
        with db(read_only=True) as con:
            row = con.execute(
                """
                SELECT
                  (SELECT COUNT(DISTINCT date) FROM bhav_daily),
                  (SELECT MIN(date) FROM bhav_daily),
                  (SELECT MAX(date) FROM bhav_daily),
                  (SELECT COUNT(*) FROM bhav_daily),
                  (SELECT COUNT(DISTINCT symbol) FROM bhav_daily),
                  (SELECT COUNT(DISTINCT as_of_date) FROM universe_rank),
                  (SELECT COUNT(*) FROM adj_events),
                  (SELECT COUNT(DISTINCT symbol) FROM adj_events)
                """
            ).fetchone()
        (
            stats["trading_days"], stats["first_date"], stats["last_date"],
            stats["rows_bhav"], stats["distinct_symbols"],
            stats["rank_snapshots"], stats["adj_events"],
            stats["symbols_with_actions"],
        ) = row
        if stats["first_date"]:
            stats["first_date"] = str(stats["first_date"])
        if stats["last_date"]:
            stats["last_date"] = str(stats["last_date"])
        if stats.get("rows_bhav"):
            stats["rows_bhav_display"] = f"{stats['rows_bhav']/1e6:.1f}M"
    except Exception as e:
        stats["duckdb_error"] = str(e)[:200]

    # size on disk
    def dir_size(p: Path) -> int:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    try:
        stats["total_size_mb"] = round(
            (dir_size(RAW_DIR) + dir_size(PARQUET_DIR) + dir_size(ACTIONS_DIR)) / (1024 * 1024)
        )
    except Exception:
        pass

    return stats


# ───────────────────── per-year HTML pages ───────────────────────────

YEAR_PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{year} · Browse · nse-universe</title>
  <meta name="description" content="NSE bhavcopy files for {year} — {day_count} trading days. Direct download links for raw zip and normalized Parquet." />
  <link rel="canonical" href="{site_url}/browse/{year}/" />
  <meta name="robots" content="index, follow" />

  <meta property="og:type" content="article" />
  <meta property="og:title" content="NSE bhavcopy {year} — nse-universe" />
  <meta property="og:description" content="{day_count} trading days in {year}. Raw zip + Parquet downloads." />
  <meta property="og:url" content="{site_url}/browse/{year}/" />
  <meta property="og:image" content="{site_url}/og.png" />
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:image" content="{site_url}/og.png" />

  <link rel="icon" type="image/svg+xml" href="../../assets/favicon.svg" />
  <link rel="stylesheet" href="../../assets/style.css" />
</head>
<body>
  <div class="page wide">
    <header class="site">
      <a class="brand" href="../../">nse-universe</a>
      <nav>
        <a href="../../">Overview</a>
        <a href="../" class="active">Browse</a>
        <a href="../../api.html">API</a>
        <a href="{repo_url}">GitHub</a>
      </nav>
    </header>

    <main>
      <p><a href="../">← All years</a></p>
      <h1>{year}</h1>
      <p class="muted">{day_count} trading days. Direct download links below — zip is the original NSE file, parquet is the normalized derivative.</p>

      <table>
        <thead>
          <tr><th>date</th><th>raw zip</th><th class="num">zip size</th><th>parquet</th><th class="num">parquet size</th></tr>
        </thead>
        <tbody>
{rows}
        </tbody>
      </table>
    </main>

    <footer class="site">
      <span><a href="../">← all years</a></span>
      <span>{site_url}/browse/{year}/</span>
    </footer>
  </div>
</body>
</html>
"""

def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    return f"{n/(1024*1024):.2f} MB"


def render_year_page(cfg: SiteConfig, year: str, year_info: dict) -> str:
    rows = []
    for d in year_info["days"]:
        iso = d["date"]
        zip_path = "../../" + d["zip"]
        pq_path = "../../" + d["parquet"] if "parquet" in d else ""
        pq_cell = (
            f'<a href="{pq_path}#" download>download</a>' if pq_path else "<span class=\"muted\">—</span>"
        )
        rows.append(
            f'          <tr id="{iso}"><td><code>{iso}</code></td>'
            f'<td><a href="{zip_path}" download>download</a></td>'
            f'<td class="num">{_fmt_bytes(d.get("zip_bytes"))}</td>'
            f'<td>{pq_cell}</td>'
            f'<td class="num">{_fmt_bytes(d.get("parquet_bytes"))}</td></tr>'
        )
    return YEAR_PAGE_TEMPLATE.format(
        year=year,
        day_count=year_info["count"],
        site_url=cfg.site_url,
        repo_url=cfg.repo_url,
        rows="\n".join(rows),
    )


# ───────────────────── sitemap + placeholder substitution ───────────

def build_sitemap(cfg: SiteConfig, manifest: dict) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    urls = [
        (cfg.site_url + "/",          today, "weekly",  "1.0"),
        (cfg.site_url + "/browse/",   today, "daily",   "0.9"),
        (cfg.site_url + "/api.html",  today, "monthly", "0.7"),
    ]
    for yr in sorted(manifest["years"], reverse=True):
        urls.append((f"{cfg.site_url}/browse/{yr}/", today, "weekly", "0.6"))

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod, changefreq, prio in urls:
        parts.append(
            f"  <url><loc>{loc}</loc><lastmod>{lastmod}</lastmod>"
            f"<changefreq>{changefreq}</changefreq><priority>{prio}</priority></url>"
        )
    parts.append("</urlset>")
    return "\n".join(parts) + "\n"


def substitute_placeholders(cfg: SiteConfig) -> int:
    """Replace {{SITE_URL}} and {{REPO_URL}} in every HTML/txt/xml file under docs/."""
    pat_site = re.compile(r"\{\{SITE_URL\}\}")
    pat_repo = re.compile(r"\{\{REPO_URL\}\}")
    count = 0
    for p in DOCS.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".html", ".txt", ".xml", ".json"}:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new = pat_repo.sub(cfg.repo_url, pat_site.sub(cfg.site_url, text))
        if new != text:
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(new, encoding="utf-8")
            tmp.replace(p)
            count += 1
    return count


# ───────────────────── OG image ───────────────────────────────────────

def build_og_image(cfg: SiteConfig, stats: dict) -> Path:
    """Render 1200x630 OG card via Pillow.

    Mono typography, dark palette, dataset stats front and center.
    """
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1200, 630
    bg = (13, 13, 12)          # near-black
    text = (232, 230, 224)
    accent = (226, 106, 80)
    muted = (139, 138, 132)
    border = (38, 38, 37)

    # Pillow reads TTF, not WOFF2 — we ship a TTF copy next to this script
    script_dir = Path(__file__).parent
    ttf_regular = script_dir / "JetBrainsMono-Regular.ttf"
    ttf_bold = script_dir / "JetBrainsMono-Bold.ttf"

    def _font(path: Path, size: int) -> ImageFont.ImageFont:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            return ImageFont.load_default()

    f_title = _font(ttf_bold, 64)
    f_sub = _font(ttf_regular, 28)
    f_stat_value = _font(ttf_bold, 44)
    f_stat_label = _font(ttf_regular, 18)
    f_foot = _font(ttf_regular, 22)

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    # accent bar
    d.rectangle([(0, 0), (W, 8)], fill=accent)

    # brand
    d.text((60, 60), "▸ nse-universe", fill=text, font=f_title)
    d.text((60, 150), "Survivorship-free NSE bhavcopy + point-in-time", fill=muted, font=f_sub)
    d.text((60, 190), "custom index membership, 2005 → today.", fill=muted, font=f_sub)

    def _fmt_int(v, default):
        try:
            return f"{int(v):,}"
        except (TypeError, ValueError):
            return str(default)

    td = _fmt_int(stats.get("trading_days"), "5,256")
    sym = _fmt_int(stats.get("distinct_symbols"), "4,166")
    rk = _fmt_int(stats.get("rank_snapshots"), "249")
    rows_disp = stats.get("rows_bhav_display") or "7.5M"
    size_mb = stats.get("total_size_mb")
    size_disp = f"{size_mb} MB" if size_mb else "680 MB"
    first = stats.get("first_date", "2005")
    last = stats.get("last_date", "")
    cov = f"{first[:4]}→{last[:4]}" if first and last else "21+ yrs"

    stat_entries = [
        (td, "trading days"),
        (sym, "symbols"),
        (rk, "monthly ranks"),
        (cov, "coverage"),
        (rows_disp, "bhav rows"),
        (size_disp, "total size"),
    ]
    col_w = (W - 120) // 3
    y0 = 320
    for i, (val, label) in enumerate(stat_entries):
        col = i % 3
        row = i // 3
        x = 60 + col * col_w
        y = y0 + row * 130
        d.rectangle([(x, y), (x + col_w - 16, y + 110)], outline=border, width=1)
        d.text((x + 20, y + 18), str(val), fill=text, font=f_stat_value)
        d.text((x + 20, y + 75), label.upper(), fill=muted, font=f_stat_label)

    # footer url
    footer_host = cfg.site_url.replace("https://", "").replace("http://", "")
    d.text((60, H - 50), footer_host, fill=accent, font=f_foot)

    out = DOCS / "og.png"
    tmp = out.with_suffix(".png.tmp")
    img.save(tmp, "PNG", optimize=True)
    tmp.replace(out)
    return out


# ───────────────────── main ───────────────────────────────────────────

def main() -> None:
    cfg = SiteConfig.load()
    DOCS.mkdir(parents=True, exist_ok=True)

    # CI may not have DuckDB. Rebuild it from committed parquet + derived.
    print(f"[0/6] bootstrapping DuckDB from parquet (if empty) …")
    try:
        stats0 = rebuild_from_parquet()
        print(f"      bhav: rows={stats0['rows']}, symbols={stats0['symbols']}")
    except Exception as e:
        print(f"      rebuild_from_parquet: {type(e).__name__}: {e}")
    try:
        imp = import_all_if_missing()
        if imp["universe_rank"] or imp["adj_events"]:
            print(f"      imported universe_rank={imp['universe_rank']}, adj_events={imp['adj_events']}")
    except Exception as e:
        print(f"      import_all_if_missing: {type(e).__name__}: {e}")

    print(f"[1/6] building manifest …")
    manifest = build_manifest(cfg)
    total_days = sum(y["count"] for y in manifest["years"].values())
    (DOCS / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
    print(f"      {total_days:,} days across {len(manifest['years'])} years")

    print(f"[2/6] building stats …")
    stats = build_stats(cfg, manifest)
    (DOCS / "stats.json").write_text(json.dumps(stats, indent=1, sort_keys=True))
    print(f"      trading_days={stats.get('trading_days')} symbols={stats.get('distinct_symbols')}")

    print(f"[3/6] rendering per-year pages …")
    for yr, info in manifest["years"].items():
        out_dir = DOCS / "browse" / yr
        out_dir.mkdir(parents=True, exist_ok=True)
        html = render_year_page(cfg, yr, info)
        (out_dir / "index.html").write_text(html, encoding="utf-8")
    print(f"      wrote {len(manifest['years'])} year pages")

    print(f"[4/6] building sitemap.xml …")
    (DOCS / "sitemap.xml").write_text(build_sitemap(cfg, manifest), encoding="utf-8")

    print(f"[5/6] generating og.png …")
    og = build_og_image(cfg, stats)
    print(f"      {og.relative_to(REPO_ROOT)} ({og.stat().st_size/1024:.1f} KB)")

    print(f"[6/6] substituting URL placeholders …")
    n = substitute_placeholders(cfg)
    print(f"      rewrote {n} files with SITE_URL={cfg.site_url}")

    print("\ndone. commit docs/ and push to deploy.")


if __name__ == "__main__":
    main()
