// The upload of a .wpress: the file is the raw body of one PUT to /import/files/<name>, with the page's token in a header. Nothing here builds HTML: every message is set with textContent.
(function () {
  "use strict";
  var form = document.getElementById("upload");
  if (!form) { return; }
  var picker = document.getElementById("upload-file");
  var nameBox = document.getElementById("upload-name");
  var bar = document.getElementById("upload-progress");
  var status = document.getElementById("upload-status");
  var runFile = document.getElementById("run-file");
  var NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*\.wpress$/;
  var busy = false;

  function say(text, bad) {
    status.textContent = text;
    status.className = bad ? "error" : "";
  }

  picker.addEventListener("change", function () {
    if (picker.files.length && !nameBox.value) {
      nameBox.value = picker.files[0].name.replace(/[^A-Za-z0-9._-]/g, "-").replace(/^[^A-Za-z0-9]+/, "");
    }
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (busy) { return; }
    var file = picker.files[0];
    var name = nameBox.value;
    if (!file) { say("Choose a file first.", true); return; }
    if (!NAME.test(name) || name.length > 100) { say("The name on dellwp is letters, digits, dot, dash and underscore, and ends in .wpress.", true); return; }
    if (file.size < 1) { say("That file is empty.", true); return; }
    if (file.size > Number(form.dataset.maxBytes)) { say("That file is larger than the intake can hold.", true); return; }
    var xhr = new XMLHttpRequest();
    xhr.open("PUT", form.dataset.url + encodeURIComponent(name));
    xhr.setRequestHeader("X-CSRF", form.dataset.csrf);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) {
        bar.hidden = false;
        bar.value = Math.round(100 * e.loaded / e.total);
        say("Sending: " + Math.round(e.loaded / 1048576) + " of " + Math.round(e.total / 1048576) + " MB", false);
      }
    };
    xhr.onload = function () {
      busy = false;
      var reply = null;
      try { reply = JSON.parse(xhr.responseText); } catch (e) { reply = null; }
      if (xhr.status === 200 && reply && reply.ok) {
        bar.value = 100;
        say("Uploaded " + reply.file + ": " + reply.bytes + " bytes, sha256 " + reply.sha256 + ". dellwp reported the same count and hash.", false);
        if (runFile) { runFile.value = reply.file; }
        if (form.dataset.reload) {
          say("Uploaded " + reply.file + ": " + reply.bytes + " bytes, sha256 " + reply.sha256 + ". dellwp reported the same count and hash. Opening the list…", false);
          window.setTimeout(function () { window.location.href = form.dataset.reload + "?file=" + encodeURIComponent(reply.file) + "&uploaded=1#start"; }, 1500);
        }
      } else {
        bar.hidden = true;
        say(reply && reply.error ? reply.error : "The upload failed (status " + xhr.status + "). If you were logged out, log in again.", true);
      }
    };
    xhr.onerror = function () { busy = false; bar.hidden = true; say("The connection broke before the upload ended. Whatever reached dellwp is removed; try again.", true); };
    xhr.onabort = xhr.onerror;
    busy = true;
    bar.value = 0;
    say("Starting…", false);
    xhr.send(file);
  });
})();
