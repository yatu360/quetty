// Keep the sessions table's horizontal scroll position across the 2-second HTMX
// refresh. The polled #session-results block is replaced with outerHTML, which would
// otherwise reset the new .table-wrap to scrollLeft = 0 on every poll.
(function () {
  "use strict";
  var WRAP = "#session-results .table-wrap";
  var saved = null;

  function swapsSessions(event) {
    var target = event.detail && event.detail.target;
    return Boolean(target && target.id === "session-results");
  }

  document.addEventListener("htmx:beforeSwap", function (event) {
    if (!swapsSessions(event)) {
      return;
    }
    var wrap = document.querySelector(WRAP);
    saved = wrap ? wrap.scrollLeft : null;
  });

  document.addEventListener("htmx:afterSwap", function () {
    if (saved === null) {
      return;
    }
    var wrap = document.querySelector(WRAP);
    if (wrap) {
      wrap.scrollLeft = saved;
    }
    saved = null;
  });
})();

// Show <time class="local-time" datetime="…Z"> values in the viewer's own timezone
// (the machine running the browser), including its GMT/BST-style abbreviation. The
// server always sends the exact UTC instant; the element text is only a fallback.
(function () {
  "use strict";
  var FORMAT = new Intl.DateTimeFormat(undefined, {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "short",
  });

  function localize(root) {
    var nodes = (root || document).querySelectorAll("time.local-time[datetime]");
    for (var index = 0; index < nodes.length; index += 1) {
      var node = nodes[index];
      var instant = new Date(node.getAttribute("datetime"));
      if (!isNaN(instant.getTime())) {
        node.textContent = FORMAT.format(instant);
        node.title = instant.toISOString();
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { localize(document); });
  } else {
    localize(document);
  }
  document.addEventListener("htmx:afterSwap", function () { localize(document); });
})();
