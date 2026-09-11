// Plot Armor Facts pages - shared helpers. Reads the public repo through the GitHub API, no build step.
const PAF = (() => {
  const params = new URLSearchParams(location.search);
  let owner = "ajmaladev";
  let repo = "youtube-automations";
  if (location.hostname.endsWith(".github.io")) {
    owner = location.hostname.split(".")[0];
    const first = location.pathname.split("/").filter(Boolean)[0];
    if (first && !first.endsWith(".html")) repo = first;
  }
  const cfg = {
    owner: params.get("owner") || owner,
    repo: params.get("repo") || repo,
    ref: params.get("ref") || "main", // branch the calendar and workflow are read from
  };
  const refPath = (ref) => ref.split("/").map(encodeURIComponent).join("/");
  const api = (path) => `https://api.github.com/repos/${cfg.owner}/${cfg.repo}${path}`;
  const raw = (ref, path) => `https://raw.githubusercontent.com/${cfg.owner}/${cfg.repo}/${refPath(ref)}/${path}`;
  const repoUrl = (path = "") => `https://github.com/${cfg.owner}/${cfg.repo}${path}`;

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : String(child));
    }
    return node;
  }

  function storage(kind) {
    try {
      const store = window[kind];
      store.setItem("paf:probe", "1");
      store.removeItem("paf:probe");
      return store;
    } catch {
      return null;
    }
  }
  const session = storage("sessionStorage");
  const local = storage("localStorage");

  async function getJSON(url, { token = "", cacheSeconds = 120 } = {}) {
    const key = `paf:${url}`;
    if (session && cacheSeconds > 0) {
      try {
        const hit = JSON.parse(session.getItem(key));
        if (hit && Date.now() - hit.at < cacheSeconds * 1000) return hit.data;
      } catch { /* ignore a bad cache entry */ }
    }
    const headers = {};
    if (url.startsWith("https://api.github.com")) {
      headers.Accept = "application/vnd.github+json";
      if (token) headers.Authorization = `Bearer ${token}`;
    }
    const response = await fetch(url, { headers, cache: "no-store" });
    if (response.status === 404) return null;
    if (response.status === 403 || response.status === 429) {
      throw new Error("GitHub's hourly limit for reading without a token was reached. Wait a bit, or add a token on the Status page.");
    }
    if (!response.ok) throw new Error(`GitHub returned ${response.status} for ${url}`);
    const data = await response.json();
    if (session && cacheSeconds > 0) {
      try { session.setItem(key, JSON.stringify({ at: Date.now(), data })); } catch { /* storage full */ }
    }
    return data;
  }

  // Every series from every topics/calendar/YYYY-MM.json on the branch, sorted by date.
  async function loadCalendar() {
    const listing = await getJSON(api(`/contents/topics/calendar?ref=${encodeURIComponent(cfg.ref)}`), { cacheSeconds: 300 });
    if (!Array.isArray(listing)) throw new Error(`No topics/calendar folder on the "${cfg.ref}" branch yet.`);
    const months = listing.filter((f) => /^\d{4}-\d{2}\.json$/.test(f.name)).map((f) => f.name).sort();
    const docs = await Promise.all(months.map((name) => getJSON(raw(cfg.ref, `topics/calendar/${name}`), { cacheSeconds: 300 })));
    const series = [];
    docs.forEach((doc) => (doc?.series || []).forEach((s) => series.push({ series: s, month: doc })));
    return series.sort((a, b) => a.series.date.localeCompare(b.series.date));
  }

  function dayIn(timeZone, offsetDays = 0) {
    const when = new Date(Date.now() + offsetDays * 86400000);
    const zone = !timeZone || timeZone === "local" ? undefined : timeZone;
    return new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" }).format(when);
  }

  function prettyDay(isoDate, style = "long") {
    const options = style === "short"
      ? { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" }
      : { weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC" };
    return new Date(`${isoDate}T12:00:00Z`).toLocaleDateString(undefined, options);
  }

  function prettyTime(isoTimestamp) {
    return new Date(isoTimestamp).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }

  // Yesterday / Today / Tomorrow buttons plus a select of every planned day. Calls onChange(entry, isoDate).
  function dayPicker(container, entries, onChange) {
    const tz = entries[0]?.month?.timezone || "local";
    const byDate = new Map(entries.map((e) => [e.series.date, e]));
    const quick = [["Yesterday", -1], ["Today", 0], ["Tomorrow", 1]];
    const buttons = quick.map(([label, offset]) => el("button", { type: "button", "data-offset": offset }, label));
    const select = el("select", { "aria-label": "Choose a day" },
      entries.map((e) => el("option", { value: e.series.date }, `${prettyDay(e.series.date, "short")} - ${e.series.series_title}`)));
    container.replaceChildren(el("div", { class: "seg", role: "group", "aria-label": "Quick days" }, buttons), select);

    function choose(isoDate) {
      if (byDate.has(isoDate)) select.value = isoDate;
      buttons.forEach((b) => b.setAttribute("aria-pressed", String(dayIn(tz, Number(b.dataset.offset)) === isoDate)));
      history.replaceState(null, "", `${location.pathname}${location.search}#${isoDate}`);
      onChange(byDate.get(isoDate) || null, isoDate);
    }
    buttons.forEach((b) => b.addEventListener("click", () => choose(dayIn(tz, Number(b.dataset.offset)))));
    select.addEventListener("change", () => choose(select.value));
    const fromHash = location.hash.slice(1);
    choose(/^\d{4}-\d{2}-\d{2}$/.test(fromHash) ? fromHash : dayIn(tz, 0));
    return { choose, timeZone: tz };
  }

  const token = {
    get: () => local?.getItem("paf:github-token") || "",
    set: (value) => local?.setItem("paf:github-token", value),
    clear: () => local?.removeItem("paf:github-token"),
  };

  return { cfg, api, raw, repoUrl, el, getJSON, loadCalendar, dayIn, prettyDay, prettyTime, dayPicker, token, persistent: Boolean(local) };
})();
