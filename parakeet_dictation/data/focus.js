// Resident KWin script: report the active window to Parakeet Dictation.
//
// KDE Wayland gives clients no way to ask which window has focus, so the
// compositor tells us instead.  Everything is passed as a string (the pid
// too) so the D-Bus signature is fixed at "ssss" whatever QJSValue would
// otherwise make of a number.  Loaded and unloaded by FocusTracker
// (parakeet_dictation/focus.py); it does not survive a KWin restart.
function rep(w) {
  var rc = "", rn = "", cap = "", pid = "-1";
  if (w) {
    rc = String(w.resourceClass || "");
    rn = String(w.resourceName || "");
    cap = String(w.caption || "");
    pid = String(w.pid !== undefined ? w.pid : -1);
  }
  callDBus("org.kde.parakeet.Focus", "/Focus", "org.kde.parakeet.Focus",
           "Report", rc, rn, cap, pid);
}
workspace.windowActivated.connect(rep);
rep(workspace.activeWindow);
