(function () {
  "use strict";

  const index = Array.isArray(window.X21_SEARCH_INDEX) ? window.X21_SEARCH_INDEX : [];
  const pageSize = 25;
  const form = document.getElementById("search-form");
  const input = document.getElementById("archive-search");
  const clearButton = document.getElementById("search-clear");
  const status = document.getElementById("search-status");
  const results = document.getElementById("search-results");
  const pagination = document.getElementById("search-pagination");
  const labels = { post: "Post", thread: "Wątek", user: "Użytkownik", forum: "Dział" };
  const normalize = (value) => String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[łŁ]/g, "l")
    .toLocaleLowerCase("pl-PL");

  function score(item, query) {
    const title = normalize(item.title);
    const author = normalize(item.author);
    const forum = normalize(item.forum);
    const text = normalize(item.text);
    let value = 0;
    let matched = false;
    if (title === query) { value += 150; matched = true; }
    else if (title.indexOf(query) === 0) { value += 110; matched = true; }
    else if (title.indexOf(query) !== -1) { value += 80; matched = true; }
    if (author === query) { value += 100; matched = true; }
    else if (author.indexOf(query) !== -1) { value += 45; matched = true; }
    if (forum.indexOf(query) !== -1) { value += 25; matched = true; }
    if (text.indexOf(query) !== -1) { value += 20; matched = true; }
    if (!matched) return 0;
    return value + (item.type === "post" ? 4 : item.type === "thread" ? 3 : item.type === "user" ? 2 : 1);
  }

  function matchRange(source, query, fromNormalized) {
    const normalizedQuery = normalize(query);
    if (!normalizedQuery) return null;
    const chars = Array.from(String(source || ""));
    const segments = [];
    let normalizedOffset = 0;
    let originalOffset = 0;
    chars.forEach((char) => {
      const normalized = normalize(char);
      segments.push({ originalStart: originalOffset, normalizedStart: normalizedOffset, normalizedEnd: normalizedOffset + normalized.length });
      normalizedOffset += normalized.length;
      originalOffset += char.length;
    });
    const foundAt = normalize(source).indexOf(normalizedQuery, fromNormalized || 0);
    if (foundAt < 0) return null;
    const foundEnd = foundAt + normalizedQuery.length;
    const first = segments.find((segment) => segment.normalizedEnd > foundAt);
    const last = segments.find((segment) => segment.normalizedStart >= foundEnd);
    if (!first) return null;
    return {
      start: first.originalStart,
      end: last ? last.originalStart : String(source || "").length,
      normalizedStart: foundAt,
      normalizedEnd: foundEnd,
    };
  }

  function appendHighlighted(parent, source, query) {
    const text = String(source || "");
    let originalOffset = 0;
    while (originalOffset < text.length) {
      const range = matchRange(text.slice(originalOffset), query);
      if (!range) break;
      const start = originalOffset + range.start;
      const end = originalOffset + range.end;
      parent.append(document.createTextNode(text.slice(originalOffset, start)));
      const mark = document.createElement("mark");
      mark.textContent = text.slice(start, end);
      parent.append(mark);
      originalOffset = end;
      if (end <= start) break;
    }
    parent.append(document.createTextNode(text.slice(originalOffset)));
  }

  function appendSnippet(parent, item, query) {
    const source = item.text || item.title || item.author || item.forum || "";
    const range = matchRange(source, query);
    if (!range) {
      appendHighlighted(parent, source.slice(0, 220), query);
      if (source.length > 220) parent.append(document.createTextNode("…"));
      return;
    }
    const start = Math.max(0, range.start - 90);
    const end = Math.min(source.length, Math.max(range.end + 110, start + 220));
    if (start > 0) parent.append(document.createTextNode("…"));
    appendHighlighted(parent, source.slice(start, end), query);
    if (end < source.length) parent.append(document.createTextNode("…"));
  }

  function resultLabel(count) {
    if (count === 1) return "wynik";
    if (count % 10 >= 2 && count % 10 <= 4 && (count % 100 < 12 || count % 100 > 14)) return "wyniki";
    return "wyników";
  }

  function stateFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const page = Math.max(1, Number.parseInt(params.get("page") || "1", 10) || 1);
    return { query: params.get("q") || "", page };
  }

  function setUrl(query, page, mode) {
    const params = new URLSearchParams();
    if (query) params.set("q", query);
    if (page > 1) params.set("page", String(page));
    const suffix = params.toString();
    window.history[mode]({}, "", window.location.pathname + (suffix ? `?${suffix}` : ""));
  }

  function renderPagination(totalPages, currentPage, rawQuery) {
    pagination.replaceChildren();
    pagination.hidden = totalPages <= 1;
    if (totalPages <= 1) return;
    const addLink = (label, page, disabled, current) => {
      const link = document.createElement("a");
      link.href = `search.html?q=${encodeURIComponent(rawQuery)}&page=${page}`;
      link.textContent = label;
      link.dataset.page = String(page);
      if (disabled) link.setAttribute("aria-disabled", "true");
      if (current) link.setAttribute("aria-current", "page");
      pagination.append(link);
    };
    addLink("‹ Poprzednia", currentPage - 1, currentPage === 1, false);
    for (let page = 1; page <= totalPages; page += 1) {
      if (totalPages > 9 && page > 2 && page < totalPages - 1 && Math.abs(page - currentPage) > 1) continue;
      addLink(String(page), page, false, page === currentPage);
    }
    addLink("Następna ›", currentPage + 1, currentPage === totalPages, false);
  }

  function render(rawQuery, requestedPage) {
    const query = normalize(rawQuery);
    const allMatches = query.length < 2 ? [] : index.map((item) => ({ item, score: score(item, query) }))
      .filter((entry) => entry.score > 0)
      .sort((a, b) => b.score - a.score || Number(b.item.timestamp || 0) - Number(a.item.timestamp || 0));
    const totalPages = Math.max(1, Math.ceil(allMatches.length / pageSize));
    const page = Math.min(Math.max(1, requestedPage || 1), totalPages);
    const visible = allMatches.slice((page - 1) * pageSize, page * pageSize);
    results.replaceChildren();
    clearButton.hidden = !rawQuery;
    if (query.length < 2) status.textContent = "Wpisz co najmniej 2 znaki.";
    else status.textContent = allMatches.length
      ? `Znaleziono ${allMatches.length} ${resultLabel(allMatches.length)} · strona ${page} z ${totalPages}.`
      : "Brak wyników dla tej frazy.";
    visible.forEach(({ item }) => {
      const row = document.createElement("li");
      row.className = "search-result";
      const heading = document.createElement("h2");
      const link = document.createElement("a");
      link.href = item.url;
      appendHighlighted(link, item.title || item.author || item.forum, rawQuery);
      heading.append(link);
      const meta = document.createElement("p");
      meta.className = "search-result-meta";
      meta.append(document.createTextNode(labels[item.type] || "Wynik"));
      [[item.author, true], [item.forum, true], [item.date, false]].forEach(([value, highlight]) => {
        if (!value) return;
        meta.append(document.createTextNode(" · "));
        if (highlight) appendHighlighted(meta, value, rawQuery); else meta.append(document.createTextNode(value));
      });
      const excerpt = document.createElement("p");
      excerpt.className = "search-result-excerpt";
      appendSnippet(excerpt, item, rawQuery);
      row.append(heading, meta, excerpt);
      results.append(row);
    });
    renderPagination(totalPages, page, rawQuery);
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const rawQuery = input.value.trim();
    setUrl(rawQuery, 1, "pushState");
    render(rawQuery, 1);
  });
  input.addEventListener("input", () => render(input.value.trim(), 1));
  clearButton.addEventListener("click", () => {
    input.value = "";
    setUrl("", 1, "pushState");
    render("", 1);
    input.focus();
  });
  pagination.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-page]");
    if (!link || link.getAttribute("aria-disabled") === "true") { event.preventDefault(); return; }
    event.preventDefault();
    const page = Number.parseInt(link.dataset.page, 10);
    const rawQuery = input.value.trim();
    setUrl(rawQuery, page, "pushState");
    render(rawQuery, page);
    results.scrollIntoView({ block: "start" });
  });
  window.addEventListener("popstate", () => {
    const state = stateFromUrl();
    input.value = state.query;
    render(state.query, state.page);
  });
  const initial = stateFromUrl();
  input.value = initial.query;
  render(initial.query, initial.page);
}());
