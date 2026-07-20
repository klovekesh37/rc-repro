// Repeated /api/v1/login — auth throughput scenario.
import http from "k6/http";
import { check } from "k6";
import { URL, buildOptions, handleSummary, record, vuUser } from "./common.js";

export const options = buildOptions();
export { handleSummary };

export default function () {
  // Log in as this VU's seeded user when available — a real multi-user login
  // storm — else the admin account.
  const u = vuUser();
  const body = JSON.stringify(u
    ? { user: u.username, password: u.password }
    : { user: __ENV.RC_USER || "admin", password: __ENV.RC_PASS || "admin123" });
  const res = record(http.post(`${URL}/api/v1/login`, body, { headers: { "Content-Type": "application/json" } }));
  check(res, { "login 200": (r) => r.status === 200 });
}
