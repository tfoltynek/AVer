document.addEventListener("input", (e) => {
  const search = e.target.closest(".multiselect-list__search");
  if (!search) return;
  const list = search.closest(".multiselect-list");
  if (!list) return;
  const value = search.value.trim().toLowerCase();
  let visible = 0;
  for (const el of list.querySelectorAll("div div")) {
    const match = el.textContent.toLowerCase().includes(value);
    el.style.display = match ? "" : "none";
    if (match) visible++;
  }
  const empty = list.querySelector(".multiselect-list__empty");
  if (empty) empty.hidden = !(value && visible === 0);
});
