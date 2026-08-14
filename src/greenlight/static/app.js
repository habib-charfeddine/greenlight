/* Greenlight dashboard — vanilla JS. The 06 spec suggests htmx from a CDN;
   the demo must run fully offline, so this file does the same three jobs:
   queue polling every 8s, per-finding feedback POSTs, broken-asset swaps. */
(function () {
  "use strict";

  var POLL_MS = 8000;

  function markDead(img) {
    var box = img.closest("[data-media], .thumb");
    if (box) box.classList.add("asset-dead");
  }

  function wireImages(root) {
    root.querySelectorAll("img[data-fallback]").forEach(function (img) {
      img.addEventListener("error", function () { markDead(img); });
      if (img.complete && img.naturalWidth === 0) markDead(img); /* failed before we attached */
    });
  }

  function refreshQueue() {
    var region = document.getElementById("queue-region");
    if (!region) return;
    var tenant = region.getAttribute("data-tenant");
    var url = "/partials/queue" + (tenant ? "?tenant=" + encodeURIComponent(tenant) : "");
    fetch(url).then(function (res) { return res.text(); }).then(function (html) {
      var doc = new DOMParser().parseFromString(html, "text/html");
      var freshQueue = doc.getElementById("queue-region");
      var freshCounters = doc.getElementById("header-counters");
      var liveCounters = document.getElementById("header-counters");
      if (freshQueue) { region.innerHTML = freshQueue.innerHTML; wireImages(region); }
      if (freshCounters && liveCounters) liveCounters.innerHTML = freshCounters.innerHTML;
    }).catch(function () { /* server busy or down; try again next tick */ });
  }

  function sendFeedback(btn) {
    var finding = btn.closest(".finding");
    var action = btn.getAttribute("data-feedback");
    fetch("/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        story_id: btn.getAttribute("data-story"),
        check_id: btn.getAttribute("data-check"),
        action: action
      })
    }).then(function (res) { return res.json(); }).then(function (out) {
      if (!out.ok || !finding) return;
      finding.querySelectorAll("button[data-feedback]").forEach(function (b) {
        b.classList.toggle("active", b === btn);
      });
      finding.classList.toggle("dismissed", action === "dismiss");
    }).catch(function () { /* leave buttons untouched on failure */ });
  }

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("button[data-feedback]");
    if (btn) sendFeedback(btn);
  });

  wireImages(document); /* script is deferred, so the DOM is already parsed */
  if (document.getElementById("queue-region")) setInterval(refreshQueue, POLL_MS);
})();
