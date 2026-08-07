// ============================================================================
// Nexify shared frontend config
// Both index.html (storefront) and admin.html (dashboard) load this file,
// so you only ever set your backend URL in ONE place.
// ============================================================================

// Set this to your deployed Flask backend URL once you host it, e.g.:
//   const API_BASE = "https://nexify-backend.onrender.com";
// While developing locally with `python app.py`, use:
//   const API_BASE = "http://localhost:5000";
// Leave it as "" to run the storefront in offline/demo mode (admin login
// requires a real backend, so set this before using admin.html).
const API_BASE = "https://nexify-mjff.onrender.com";
