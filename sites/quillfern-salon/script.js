// Highlights today's row in every hours table (rows carry data-day="0".."6", Sunday = 0).
(function () {
  var today = String(new Date().getDay());
  var rows = document.querySelectorAll('tr[data-day="' + today + '"]');
  for (var i = 0; i < rows.length; i++) {
    rows[i].classList.add('is-today');
  }
})();
