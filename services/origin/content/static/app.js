/* The Daily Edge - reader app. Every article, image and the homepage index is fetched through
 * the CDN; the inspector shows which cache layer served each object (X-Cache / X-Served-By). */
(function () {
  "use strict";

  const LAYERS = {
    "HIT-EDGE": ["Edge hit", "--l-edge"],
    "HIT-SHIELD": ["Shield hit", "--l-shield"],
    "MISS-ORIGIN": ["Origin", "--l-origin"],
    "MISS-ORIGIN-BYPASS": ["Origin (shield bypass)", "--l-origin"],
    "MISS-COLLAPSED": ["Collapsed", "--l-collapsed"],
    "STALE-EDGE": ["Stale (edge)", "--l-stale"],
    "STALE-SHIELD": ["Stale (shield)", "--l-stale"],
  };
  const API = window.CDN_API || "";
  const app = document.getElementById("app");
  const log = [];
  let index = null;
  let inspectorOpen = false;
  try { inspectorOpen = localStorage.getItem("cdn-inspector") === "1"; } catch (e) { /* storage blocked */ }

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const layerInfo = (layer) => LAYERS[layer] || ["Error", "--l-error"];
  const dot = (layer) => `<span class="dot" style="background:var(${layerInfo(layer)[1]})"></span>`;
  const ago = (iso) => {
    const mins = Math.max(1, Math.round((Date.now() - new Date(iso)) / 60000));
    if (mins < 60) return `${mins} min ago`;
    const hrs = Math.round(mins / 60);
    if (hrs < 48) return `${hrs} h ago`;
    return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  };

  async function cdn(path, as) {
    const started = performance.now();
    const resp = await fetch(API + path, { cache: "no-store" });
    const entry = {
      path: path.replace("/content/", "").replace("/article/", "article:"),
      layer: resp.headers.get("X-Cache") || (resp.ok ? "?" : "ERROR"),
      edge: resp.headers.get("X-Served-By") || "-",
      ms: Number(resp.headers.get("X-Latency-ms")) || performance.now() - started,
      status: resp.status,
    };
    log.unshift(entry);
    log.length = Math.min(log.length, 60);
    renderInspector();
    if (!resp.ok) throw new Error(`${resp.status} for ${path}`);
    return as === "blob" ? { blob: await resp.blob(), entry } : resp.json();
  }

  function hydrateImages(root) {
    root.querySelectorAll("img[data-src]").forEach(async (img) => {
      const src = img.dataset.src;
      img.removeAttribute("data-src");
      try {
        const { blob, entry } = await cdn(src, "blob");
        img.src = URL.createObjectURL(blob);
        const badge = img.parentElement.querySelector(".badge");
        if (badge) badge.innerHTML = `${dot(entry.layer)}${esc(entry.layer)} · ${esc(entry.edge)} · ${entry.ms.toFixed(0)}ms`;
      } catch (e) {
        img.alt = "image unavailable";
      }
    });
  }

  const media = (path, alt) =>
    `<div class="media"><img data-src="${esc(path)}" alt="${esc(alt)}" loading="lazy"><span class="badge"></span></div>`;

  function shell(inner, activeSection) {
    const today = new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
    const sections = (index ? index.sections : []).map((s) =>
      `<a href="#/section/${s}" class="${s === activeSection ? "active" : ""}">${esc(s)}</a>`).join("");
    const breaking = index ? index.articles.filter((a) => a.category === "breaking").slice(0, 4) : [];
    return `
      <div class="wrap">
        <div class="topbar"><span>${today} · Port Halden</span>
          <span><a href="dashboard.html">CDN dashboard →</a></span></div>
        <header class="masthead"><a href="#/"><img src="${API}/content/static:logo.svg" alt=""><h1>The Daily Edge</h1></a>
          <div class="tagline">News delivered from the nearest edge</div></header>
        <nav class="sections"><a href="#/" class="${activeSection ? "" : "active"}">Front page</a>${sections}</nav>
        ${breaking.length ? `<div class="ticker"><strong>BREAKING</strong><div class="items">${breaking.map((a) =>
          `<a href="#/article/${a.article_id}">${esc(a.title.replace("BREAKING: ", ""))}</a>`).join(" &nbsp;•&nbsp; ")}</div></div>` : ""}
        ${inner}
      </div>
      <footer>The Daily Edge is a fictional newspaper used as the workload for an Edge CDN + Origin Shield demo.</footer>`;
  }

  function card(a) {
    return `<a class="card" href="#/article/${a.article_id}">${media(a.thumbnail_path, a.title)}
      <div class="kicker" style="margin-top:10px">${esc(a.section)}</div>
      <h3>${esc(a.title)}</h3><p>${esc(a.summary)}</p><div class="meta">${ago(a.created_at)} · ${a.read_minutes} min read</div></a>`;
  }

  function renderFront(section) {
    const list = section ? index.articles.filter((a) => a.section === section) : index.articles;
    if (!list.length) { app.innerHTML = shell(`<p class="notice">No stories in this section yet.</p>`, section); return; }
    const lead = list[0];
    const popular = index.articles.filter((a) => a.category === "popular").slice(0, 6);
    const latest = index.articles.filter((a) => a !== lead).slice(0, 4);
    app.innerHTML = shell(`
      <section class="front">
        <div>
          <a class="lead" href="#/article/${lead.article_id}">${media(lead.image_path, lead.title)}
            <div class="kicker" style="margin-top:12px">${esc(lead.category === "breaking" ? "Breaking · " + lead.section : lead.section)}</div>
            <h2>${esc(lead.title)}</h2><p>${esc(lead.summary)}</p>
            <div class="meta">By ${esc(lead.author)} · ${ago(lead.created_at)}</div></a>
          <div class="grid">${list.slice(1, 13).map(card).join("")}</div>
        </div>
        <aside>
          <h4>Most read</h4>
          <ol class="ranked">${popular.map((a) => `<li><a href="#/article/${a.article_id}"><div class="title">${esc(a.title)}</div>
            <div class="meta">${esc(a.section)}</div></a></li>`).join("")}</ol>
          <h4>Latest</h4>
          ${latest.map((a) => `<a class="mini" href="#/article/${a.article_id}">${media(a.thumbnail_path, a.title)}
            <div><div class="title">${esc(a.title)}</div><div class="meta">${ago(a.created_at)}</div></div></a>`).join("")}
        </aside>
      </section>`, section);
    hydrateImages(app);
  }

  async function renderArticle(id) {
    app.innerHTML = shell(`<p class="notice">Loading article…</p>`);
    let a;
    try { a = await cdn(`/article/${encodeURIComponent(id)}`); } catch (e) {
      app.innerHTML = shell(`<p class="notice">This story could not be delivered right now (${esc(e.message)}).</p>`);
      return;
    }
    const related = index.articles.filter((x) => x.section === a.section && x.article_id !== a.article_id).slice(0, 3);
    app.innerHTML = shell(`
      <article class="article">
        <a class="back" href="#/">← Front page</a>
        <div class="kicker">${esc(a.category === "breaking" ? "Breaking · " + a.section : a.section)}</div>
        <h1>${esc(a.title)}</h1>
        <p class="standfirst">${esc(a.summary)}</p>
        <div class="meta">By ${esc(a.author)} · ${new Date(a.created_at).toLocaleString()} · ${a.read_minutes} min read</div>
        <div style="margin:18px 0">${media(a.image_path, a.title)}</div>
        <div class="body">${a.content.map((p) => `<p>${esc(p)}</p>`).join("")}</div>
        <div class="tags">${a.tags.map((t) => `<span>#${esc(t)}</span>`).join("")}</div>
        ${related.length ? `<div class="related"><h4 class="kicker">More in ${esc(a.section)}</h4><div class="grid" style="border:0;margin-top:0;padding-top:8px">${related.map(card).join("")}</div></div>` : ""}
      </article>`, a.section);
    hydrateImages(app);
    window.scrollTo(0, 0);
  }

  function renderInspector() {
    let box = document.getElementById("inspector");
    if (!box) {
      box = document.createElement("div");
      box.id = "inspector";
      box.className = "inspector";
      document.body.appendChild(box);
      box.addEventListener("click", (ev) => {
        if (ev.target.closest("header")) {
          inspectorOpen = !inspectorOpen;
          try { localStorage.setItem("cdn-inspector", inspectorOpen ? "1" : "0"); } catch (e) { /* ignore */ }
          renderInspector();
        }
        if (ev.target.id === "badges") document.body.classList.toggle("cdn-view");
        if (ev.target.id === "reload") route();
      });
    }
    const counts = {};
    log.forEach((e) => { counts[e.layer] = (counts[e.layer] || 0) + 1; });
    box.classList.toggle("open", inspectorOpen);
    box.innerHTML = `
      <header><strong>CDN inspector</strong><span class="summary">${Object.entries(counts).map(([l, n]) =>
        `<span title="${esc(layerInfo(l)[0])}">${dot(l)}${n}</span>`).join("")}</span><span>${inspectorOpen ? "▾" : "▴"}</span></header>
      <div class="body"><table><tbody>${log.slice(0, 40).map((e) => `<tr>
        <td class="path" title="${esc(e.path)}">${esc(e.path)}</td><td>${dot(e.layer)} ${esc(e.layer)}</td>
        <td>${esc(e.edge)}</td><td style="text-align:right">${e.ms.toFixed(0)} ms</td></tr>`).join("")}</tbody></table>
        <div class="tools"><button id="badges">Toggle image badges</button><button id="reload">Reload page data</button>
          <a class="btn" href="dashboard.html">Open dashboard</a></div></div>`;
  }

  async function route() {
    try {
      index = await cdn("/content/index:latest");
    } catch (e) {
      if (!index) { app.innerHTML = `<p class="notice">The newspaper is warming up (${esc(e.message)}). Retrying…</p>`; setTimeout(route, 2000); return; }
    }
    const [, kind, arg] = (location.hash || "#/").split("/");
    if (kind === "article" && arg) return renderArticle(decodeURIComponent(arg));
    renderFront(kind === "section" ? arg : null);
  }

  window.addEventListener("hashchange", route);
  renderInspector();
  route();
})();
