// Busy state on slow actions (Mr. J calls take a few seconds) and copy-to-clipboard for drafts.
document.querySelectorAll("form").forEach(f => f.addEventListener("submit", e => {
  const b = e.submitter || f.querySelector("button[data-busy]");
  if (b && b.dataset.busy) { b.textContent = b.dataset.busy; }
  setTimeout(() => f.querySelectorAll("button").forEach(x => x.disabled = true), 0);
}));
document.querySelectorAll(".msg.draft .copy").forEach(btn => btn.addEventListener("click", () => {
  const text = btn.closest(".msg").querySelector(".msg-body").textContent;
  navigator.clipboard.writeText(text).then(() => { btn.textContent = "Copied"; });
}));
const msgs = document.querySelector(".messages");
if (msgs) msgs.scrollTop = msgs.scrollHeight;
