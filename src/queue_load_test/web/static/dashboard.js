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

// Copy URL: fetch one session's transfer URL only when the operator clicks, and hand
// it straight to the Clipboard API. The URL never enters the DOM, so the polled table
// stays transfer-URL-free. One delegated listener survives every HTMX replacement of
// #session-results; transient feedback is keyed by session_id and re-applied to the
// freshly swapped buttons until it expires.
(function () {
  "use strict";
  var LABEL = "Copy URL";
  var FEEDBACK_MS = 1500;
  var feedback = {};

  function buttonsFor(sessionId) {
    var nodes = document.querySelectorAll("button[data-copy-session]");
    var matches = [];
    for (var index = 0; index < nodes.length; index += 1) {
      if (sessionId === undefined || nodes[index].getAttribute("data-copy-session") === sessionId) {
        matches.push(nodes[index]);
      }
    }
    return matches;
  }

  function render(sessionId) {
    var buttons = buttonsFor(sessionId);
    for (var index = 0; index < buttons.length; index += 1) {
      var state = feedback[buttons[index].getAttribute("data-copy-session")];
      buttons[index].textContent = state && state.until > Date.now() ? state.text : LABEL;
    }
  }

  function settle(sessionId, text) {
    var state = { text: text, until: Date.now() + FEEDBACK_MS };
    feedback[sessionId] = state;
    render(sessionId);
    window.setTimeout(function () {
      if (feedback[sessionId] === state) {
        delete feedback[sessionId];
        render(sessionId);
      }
    }, FEEDBACK_MS);
  }

  function copy(sessionId) {
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      return Promise.reject(new Error("clipboard unavailable"));
    }
    return fetch("/sessions/" + encodeURIComponent(sessionId) + "/transfer-url", {
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "same-origin",
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("transfer URL unavailable");
        }
        return response.json();
      })
      .then(function (body) {
        if (!body || typeof body.transfer_url !== "string" || !body.transfer_url) {
          throw new Error("transfer URL unavailable");
        }
        return navigator.clipboard.writeText(body.transfer_url);
      });
  }

  document.addEventListener("click", function (event) {
    var button = event.target && event.target.closest
      ? event.target.closest("button[data-copy-session]")
      : null;
    if (!button || button.disabled) {
      return;
    }
    event.preventDefault();
    var sessionId = button.getAttribute("data-copy-session");
    copy(sessionId).then(
      function () { settle(sessionId, "Copied"); },
      // Never surface the error itself: it could carry the URL.
      function () { settle(sessionId, "Copy failed"); }
    );
  });

  document.addEventListener("htmx:afterSwap", function () { render(); });
})();

// Rename: prompt for an operator label and post it as a form value (so any Unicode
// name works), swapping the session table like other row actions. A modal prompt is
// unaffected by the 2-second refresh, which would wipe an inline text field.
(function () {
  "use strict";
  document.addEventListener("click", function (event) {
    var button = event.target && event.target.closest
      ? event.target.closest("button[data-rename-url]")
      : null;
    if (!button || button.disabled || !window.htmx) {
      return;
    }
    event.preventDefault();
    var name = window.prompt(
      "Session name (leave blank to clear)",
      button.getAttribute("data-current-name") || ""
    );
    if (name === null) {
      return;
    }
    window.htmx.ajax("POST", button.getAttribute("data-rename-url"), {
      target: "#session-results",
      swap: "outerHTML",
      values: { name: name },
    });
  });
})();
