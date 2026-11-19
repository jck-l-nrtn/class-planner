
(async () => {
  const box = document.getElementById("feedback");
  if (!box) return;
  try {
    const r = await (await fetch("/api/feedback")).json();
    if (!r.email) return;
    document.getElementById("fb-bug").href = r.bug;
    document.getElementById("fb-idea").href = r.idea;
    document.getElementById("fb-addr").textContent = r.email;
    box.hidden = false;
  } catch (_) { }
})();
