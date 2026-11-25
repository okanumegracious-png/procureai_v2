// Isolated authentication bootstrap. This file is loaded after app.js and owns
// sign-in/session restoration so unrelated dashboard errors cannot disable login.
(() => {
  const form = document.getElementById("loginForm");
  const screen = document.getElementById("loginScreen");
  const shell = document.getElementById("appShell");
  const error = document.getElementById("loginError");
  const button = form?.querySelector('button[type="submit"]');
  const showLogin = message => {
    if (screen) screen.hidden = false;
    if (shell) shell.hidden = true;
    if (error) error.textContent = message || "";
  };
  const showWorkspace = () => {
    if (screen) screen.hidden = true;
    if (shell) shell.hidden = false;
  };
  const loadWorkspace = async () => {
    const response = await fetch("/api/auth/me", {credentials:"same-origin",cache:"no-store"});
    if (!response.ok) { showLogin(); return false; }
    const session = await response.json();
    if (!session?.authenticated) { showLogin(); return false; }
    showWorkspace();
    if (typeof fetchDashboard === "function") await fetchDashboard();
    if (typeof loadCompanyProfile === "function") await loadCompanyProfile();
    if (typeof loadDatabaseBackups === "function") await loadDatabaseBackups();
    return true;
  };
  form?.addEventListener("submit", async event => {
    event.preventDefault();
    if (button) { button.disabled = true; button.textContent = "Signing in…"; }
    if (error) error.textContent = "";
    try {
      const response = await fetch("/api/auth/login", {
        method:"POST",
        credentials:"same-origin",
        cache:"no-store",
        headers:{"Content-Type":"application/json","Accept":"application/json"},
        body:JSON.stringify({
          username:document.getElementById("loginUsername").value.trim(),
          password:document.getElementById("loginPassword").value
        })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || "Unable to sign in. Check your details and try again.");
      document.getElementById("loginPassword").value = "";
      const restored = await loadWorkspace();
      if (!restored) throw new Error("Your sign-in was accepted, but the session could not be restored. Please try again.");
    } catch (err) {
      showLogin(err.message || "Sign-in failed. Please try again.");
    } finally {
      if (button) { button.disabled = false; button.textContent = "Sign in securely"; }
    }
  });
  loadWorkspace().catch(() => showLogin("Unable to verify your session. Check your connection and try again."));
})();
