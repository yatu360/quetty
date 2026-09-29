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
