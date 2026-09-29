(function () {
  "use strict";

  function pad(n) {
    return (n < 10 ? "0" : "") + n;
  }

  // Reproduce Django's "d.m.Y H:i" format in the browser's local timezone.
  function format(date) {
    return (
      pad(date.getDate()) +
      "." +
      pad(date.getMonth() + 1) +
      "." +
      date.getFullYear() +
      " " +
      pad(date.getHours()) +
      ":" +
      pad(date.getMinutes())
    );
  }

  function localize(root) {
    root = root || document;
    var found = root.querySelectorAll ? root.querySelectorAll(".js-localdt") : [];
    var nodes = Array.prototype.slice.call(found);
    if (root.matches && root.matches(".js-localdt")) {
      nodes.push(root);
    }
    nodes.forEach(function (node) {
      var iso = node.getAttribute("datetime");
      if (!iso) return;
      var date = new Date(iso);
      if (isNaN(date.getTime())) return; // leave the server fallback in place
      node.textContent = format(date);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      localize(document);
    });
  } else {
    localize(document);
  }

  // Re-localize content swapped in by HTMX (e.g. a card added after upload).
  document.body.addEventListener("htmx:load", function (event) {
    localize(event.target);
  });
})();
