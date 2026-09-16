(() => {
  const input = document.querySelector("#user-search");
  const cards = [...document.querySelectorAll("[data-user-card]")];
  const empty = document.querySelector("[data-empty-search]");

  if (!input || !empty) return;

  input.addEventListener("input", () => {
    const query = input.value.trim().toLocaleLowerCase("pl");
    let visible = 0;

    for (const card of cards) {
      const matches = card.textContent.toLocaleLowerCase("pl").includes(query);
      card.hidden = !matches;
      if (matches) visible += 1;
    }

    empty.hidden = visible !== 0;
  });
})();
