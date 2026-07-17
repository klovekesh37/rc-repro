// Hit an arbitrary REST endpoint under load — reproduce the customer's *actual*
// slow call. Method/path/body come from env (RC_METHOD, RC_PATH, RC_BODY), set
// by `rc-repro loadtest --scenario custom --endpoint "GET /api/v1/..." [--body]`.
import http from "k6/http";
import { check } from "k6";
import { URL, authParams, buildOptions, handleSummary, record } from "./common.js";

export const options = buildOptions();
export { handleSummary };

const METHOD = (__ENV.RC_METHOD || "GET").toUpperCase();
const PATH = __ENV.RC_PATH || "/api/v1/me";
const BODY = __ENV.RC_BODY || null;

export default function () {
  // GET/DELETE carry no body; POST/PUT/PATCH send RC_BODY (already JSON).
  const hasBody = METHOD !== "GET" && METHOD !== "DELETE";
  const res = record(http.request(METHOD, `${URL}${PATH}`, hasBody ? BODY : null, authParams));
  check(res, { "status < 400": (r) => r.status < 400 });
}
