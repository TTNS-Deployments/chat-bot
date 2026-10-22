// Demo sign-in page (AUTH_MODE=password). Posts the shared password and returns to the chat page.
(() => {
  "use strict";
  const form = document.getElementById("login-form");
  const input = document.getElementById("password");
  const button = document.getElementById("sign-in");
  const error = document.getElementById("login-error");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    button.disabled = true;
    try {
      const response = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: input.value }),
      });
      if (response.ok) {
        window.location.replace("/");
        return;
      }
      let message = `Sign-in failed (${response.status}).`;
      try { message = (await response.json()).error.message || message; } catch { /* not JSON */ }
      error.textContent = message;
      input.select();
    } catch {
      error.textContent = "Could not reach the server. Check your connection and try again.";
    } finally {
      button.disabled = false;
    }
  });
})();
