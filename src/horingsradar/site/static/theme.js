/* Høringsradar: apply the visitor's theme choice before the page is drawn (loaded without defer).
 * "light" or "dark" in localStorage hr.theme sets data-theme on <html>; anything else follows the system. */
(function () {
  "use strict";
  try {
    var t = window.localStorage.getItem("hr.theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) {
    /* storage unavailable: follow the system */
  }
})();
