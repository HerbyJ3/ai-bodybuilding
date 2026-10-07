// Client app enhancements. Every form already works without this file.
// No untrusted text is inserted here (only moves existing nodes / sets fixed strings).
(function () {
  // Busy state: Mr. J takes a few seconds to answer.
  document.querySelectorAll("form").forEach(f => f.addEventListener("submit", e => {
    const b = e.submitter || f.querySelector("button[data-busy]");
    if (b && b.dataset.busy) b.textContent = b.dataset.busy;
    setTimeout(() => f.querySelectorAll("button").forEach(x => { x.disabled = true; }), 0);
  }));

  // Newest message in view in each thread.
  document.querySelectorAll(".messages").forEach(m => { m.scrollTop = m.scrollHeight; });

  // Red x: clear the field next to it.
  document.querySelectorAll(".field .clear").forEach(btn => btn.addEventListener("click", () => {
    const input = btn.parentElement.querySelector("select, input");
    input.value = ""; input.focus();
  }));

  // Daily Target: off-by amounts only when "No"; rest-day targets when the box is ticked.
  document.querySelectorAll(".daily-form").forEach(f => {
    const off = f.querySelector(".off-by");
    const radios = f.querySelectorAll('input[name="hit"]');
    const syncHit = () => {
      const missed = f.querySelector('input[name="hit"]:checked')?.value === "no";
      off.hidden = !missed;
      if (!missed) off.querySelectorAll("select").forEach(s => { s.value = ""; });
    };
    radios.forEach(r => r.addEventListener("change", syncHit)); syncHit();
    const box = f.querySelector(".rest-day"), line = f.querySelector(".target-line");
    if (box && line) {
      const syncDay = () => { line.textContent = box.checked ? line.dataset.rest : line.dataset.training; };
      box.addEventListener("change", syncDay); syncDay();
    }
  });

  // Show the saved/error banner inside the section the form came from.
  const banners = document.querySelector(".banners");
  const target = location.hash && document.getElementById(location.hash.slice(1));
  if (banners && target && target.matches("section")) {
    const h = target.querySelector("h2");
    if (h) h.after(banners); else target.prepend(banners);
    banners.classList.add("in-section");
    target.scrollIntoView();
  }
})();

// Message times are stored in UTC; show them in this device's local time.
document.querySelectorAll("time.local-time").forEach(t => {
  const d = new Date(t.dateTime);
  if (!isNaN(d)) t.textContent = d.toLocaleString(undefined,
    {month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit"});
});

// Diary: picking a date in the calendar opens that day.
const dayInput = document.getElementById("day-input");
if (dayInput) dayInput.addEventListener("change", () => { if (dayInput.value) dayInput.form.submit(); });

// Settings: look (colour style) and light/dark, remembered on this device only.
(function () {
  const root = document.documentElement;
  const read = k => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const save = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private mode: not saved */ } };
  const panel = document.querySelector(".settings");
  if (!panel) return;
  const style = read("mrj-style") || "classic", mode = read("mrj-mode") || "auto";
  panel.querySelectorAll('input[name="style"]').forEach(r => {
    r.checked = r.value === style;
    r.addEventListener("change", () => {
      if (r.value === "classic") delete root.dataset.style;
      else { root.dataset.style = r.value; if (window.mrjFonts) window.mrjFonts(r.value); }
      save("mrj-style", r.value);
    });
  });
  panel.querySelectorAll('input[name="mode"]').forEach(r => {
    r.checked = r.value === mode;
    r.addEventListener("change", () => {
      if (r.value === "auto") delete root.dataset.theme; else root.dataset.theme = r.value;
      save("mrj-mode", r.value);
    });
  });
})();
