// Repeated /api/v1/login — auth throughput scenario.
import http from "k6/http";
import { check } from "k6";
import { URL, buildOptions, handleSummary, record } from "./common.js";

export const options = buildOptions();
export { handleSummary };

export default function () {
  const body = JSON.stringify({
    user: __ENV.RC_USER || "admin",
    password: __ENV.RC_PASS || "admin123",
  });
  const res = record(http.post(`${URL}/api/v1/login`, body, { headers: { "Content-Type": "application/json" } }));
  check(res, { "login 200": (r) => r.status === 200 });
}
