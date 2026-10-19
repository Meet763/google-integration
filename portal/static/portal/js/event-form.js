(function () {
  var form = document.getElementById("event-form");
  if (!form) return;

  var meet = document.getElementById("id_meet_enabled");
  var attendeesOn = document.getElementById("id_attendees_enabled");
  var attendees = document.getElementById("id_attendees");
  var summary = document.getElementById("event-summary");
  var chips = document.getElementById("attendee-chips");
  var attendeeFields = document.getElementById("attendees-fields");

  function emails() {
    if (!attendees) return [];
    return attendees.value
      .split(/[\n,]/)
      .map(function (line) { return line.trim().toLowerCase(); })
      .filter(Boolean);
  }

  function render() {
    if (attendeeFields) {
      attendeeFields.hidden = !(attendeesOn && attendeesOn.checked);
    }
    if (chips && attendees) {
      var list = emails();
      chips.innerHTML = "";
      chips.hidden = list.length === 0;
      list.forEach(function (email) {
        var item = document.createElement("li");
        item.textContent = email;
        chips.appendChild(item);
      });
    }
    if (!summary) return;
    var parts = ["Timed event on the connected calendar"];
    if (meet && meet.checked) parts.push("with a Meet link");
    if (attendeesOn && attendeesOn.checked) {
      var count = emails().length;
      parts.push(count ? "and " + count + " attendee" + (count === 1 ? "" : "s") : "and attendees still to add");
    }
    summary.textContent = parts.join(" ") + ".";
  }

  form.addEventListener("input", render);
  form.addEventListener("change", render);
  form.addEventListener("submit", function (event) {
    if (attendeesOn && attendeesOn.checked && emails().length === 0) {
      event.preventDefault();
      if (summary) summary.textContent = "Add at least one attendee email, or turn attendees off.";
    }
  });
  render();
})();
