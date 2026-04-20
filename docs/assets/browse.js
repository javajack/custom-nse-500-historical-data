// Minimal client-side manifest browser.
// Loads manifest.json once, builds a year tile grid + a date/symbol search.
(async () => {
  const SITE = document.documentElement.dataset.site || "";
  const manifestUrl = "../manifest.json";
  const statsUrl = "../stats.json";

  const countEl = document.getElementById("count");
  const resultsEl = document.getElementById("results");
  const yearsEl = document.getElementById("year-grid");
  const totalsEl = document.getElementById("totals");
  const yearsSection = document.getElementById("years-section");
  const q = document.getElementById("q");

  let manifest = null, stats = null;

  try {
    const [m, s] = await Promise.all([
      fetch(manifestUrl).then(r => r.ok ? r.json() : null).catch(() => null),
      fetch(statsUrl).then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    manifest = m;
    stats = s;
  } catch (e) {
    countEl.textContent = "manifest unavailable";
    return;
  }

  if (!manifest) {
    countEl.textContent = "manifest unavailable — run scripts/build_docs.py before deploying";
    return;
  }

  // manifest shape: { years: { "2024": { days: [{date, zip, parquet, bytes}], count }, ... } }
  const yearKeys = Object.keys(manifest.years || {}).sort((a, b) => b.localeCompare(a));
  const totalDays = yearKeys.reduce((n, y) => n + (manifest.years[y].count || manifest.years[y].days.length), 0);
  countEl.textContent = totalDays.toLocaleString() + " days indexed";

  // year grid
  for (const y of yearKeys) {
    const yr = manifest.years[y];
    const a = document.createElement("a");
    a.className = "year-tile";
    a.href = y + "/";
    a.innerHTML = `<span class="y">${y}</span><span class="d">${(yr.count || yr.days.length).toLocaleString()} days</span>`;
    yearsEl.appendChild(a);
  }

  // totals table
  if (stats) {
    const rows = [
      ["trading days", stats.trading_days],
      ["distinct symbols", stats.distinct_symbols],
      ["rows (bhav_daily)", stats.rows_bhav],
      ["monthly rank snapshots", stats.rank_snapshots],
      ["corp-action events", stats.adj_events],
      ["symbols with actions", stats.symbols_with_actions],
      ["first date", stats.first_date],
      ["last date", stats.last_date],
      ["total on disk", stats.total_size_mb ? stats.total_size_mb + " MB" : "—"],
    ];
    for (const [k, v] of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${k}</td><td class="num">${(v ?? "—").toLocaleString ? (v ?? 0).toLocaleString() : v}</td>`;
      totalsEl.appendChild(tr);
    }
  }

  // flat day list for search
  const allDays = [];
  for (const y of yearKeys) {
    for (const d of manifest.years[y].days) {
      allDays.push({ year: y, ...d });
    }
  }

  function render(matches) {
    if (!matches || matches.length === 0) {
      resultsEl.innerHTML = '<p class="muted">no matches</p>';
      return;
    }
    const rows = matches.slice(0, 500).map(d => `
      <tr>
        <td><code>${d.date}</code></td>
        <td><a href="../${d.zip}" download>zip</a> <small class="muted">${formatBytes(d.zip_bytes)}</small></td>
        <td><a href="../${d.parquet}" download>parquet</a> <small class="muted">${formatBytes(d.parquet_bytes)}</small></td>
        <td><a href="${d.year}/#${d.date}">context</a></td>
      </tr>`).join("");
    const more = matches.length > 500 ? `<p class="muted">showing first 500 of ${matches.length.toLocaleString()} matches — narrow your search</p>` : "";
    resultsEl.innerHTML = `
      <table>
        <thead><tr><th>date</th><th>raw zip</th><th>parquet</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>${more}`;
  }

  function formatBytes(n) {
    if (n == null) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  let tmo;
  q.addEventListener("input", () => {
    clearTimeout(tmo);
    tmo = setTimeout(() => {
      const v = q.value.trim().toLowerCase();
      if (!v) {
        resultsEl.classList.add("hidden");
        yearsSection.classList.remove("hidden");
        return;
      }
      const hit = allDays.filter(d => d.date.includes(v));
      resultsEl.classList.remove("hidden");
      yearsSection.classList.add("hidden");
      render(hit);
    }, 120);
  });
})();
