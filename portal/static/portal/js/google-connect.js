(function () {
  var form = document.getElementById("google-connect-form");
  if (!form) return;
  var toggle = form.querySelector("#id_google_enabled");
  var box = document.getElementById("google-services");
  var submit = document.getElementById("google-connect-submit");
  var note = document.getElementById("google-connect-note");
  if (!toggle || !box || !submit) return;
  var onLabel = submit.getAttribute("data-on") || "Enable and sign in with Google";

  function sync() {
    var on = toggle.checked;
    box.hidden = !on;
    submit.textContent = on ? onLabel : "Turn Google integration off";
    if (note) note.textContent = "";
  }

  toggle.addEventListener("change", sync);
  form.addEventListener("submit", function (event) {
    if (!toggle.checked) return;
    var checked = form.querySelectorAll('input[name="services"]:checked');
    if (!checked.length) {
      event.preventDefault();
      if (note) note.textContent = "Choose at least one Google service.";
    }
  });
  sync();
})();
