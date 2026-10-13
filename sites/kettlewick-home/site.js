/* Kettlewick Home Repair — small page helpers. The site works without this file. */
(function () {
  "use strict";

  // Resize the yellow callout tags in the house drawing to fit their text.
  function fitTags() {
    var tags = document.querySelectorAll(".tag");
    Array.prototype.forEach.call(tags, function (tag) {
      var text = tag.querySelector("text");
      var path = tag.querySelector("path");
      if (!text || !path || !text.getComputedTextLength) return;
      var w = Math.ceil(19 + text.getComputedTextLength() + 11);
      path.setAttribute("d", "M10 0H" + (w - 4) + "Q" + w + " 0 " + w + " 4V24Q" + w + " 28 " + (w - 4) + " 28H10L0 14Z");
    });
  }
  if (document.querySelector(".tag")) {
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(fitTags);
    } else {
      fitTags();
    }
  }

  // Service area: look up a ZIP code in the lists on the page.
  var zipForm = document.getElementById("zip-form");
  if (zipForm) {
    var zipInput = document.getElementById("zip-input");
    var zipResult = document.getElementById("zip-result");
    zipForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var zip = (zipInput.value || "").replace(/\s+/g, "");
      if (!/^\d{5}$/.test(zip)) {
        zipResult.dataset.state = "error";
        zipResult.textContent = "Enter a 5-digit ZIP code, like 80211.";
        return;
      }
      var item = document.querySelector('[data-zips~="' + zip + '"]');
      if (!item) {
        zipResult.dataset.state = "unknown";
        zipResult.textContent = zip + " isn't on our list. Send a quote request anyway and Jo will tell you whether we can get to you.";
        return;
      }
      var zone = item.closest("[data-zone]").getAttribute("data-zone");
      var label = item.querySelector("span") ? item.querySelector("span").textContent : "";
      // Labels can contain their own parentheses ("Lakewood (central)"), so append them after a colon, not in "(…)".
      var places = label ? ": " + label : "";
      var messages = {
        core: zip + " is in our core area" + places + ". No travel fee, and after-hours emergency plumbing is available.",
        extended: zip + " is in our extended area" + places + ". A $35 travel fee applies to each visit, and we handle emergencies there during regular hours.",
        outside: zip + " is outside our service area" + places + ". Call the office at (720) 555-0142 and we'll refer you to a licensed contractor nearby."
      };
      zipResult.dataset.state = zone;
      zipResult.textContent = messages[zone];
    });
  }

  // Contact: the quote form has no server behind it on this static site.
  var quoteForm = document.getElementById("quote-form");
  if (quoteForm) {
    quoteForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!quoteForm.checkValidity()) {
        quoteForm.reportValidity();
        return;
      }
      var sent = document.getElementById("quote-sent");
      quoteForm.hidden = true;
      sent.hidden = false;
      sent.focus();
    });
  }
})();
