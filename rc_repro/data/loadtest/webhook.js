// Incoming-webhook storm — a monitoring system spamming a channel through an
// integration, one of the most common real-world overload patterns. rc-repro
// creates the webhook before the run and passes its path via RC_HOOK_PATH;
// webhook posts are token-authenticated, so no auth headers.
import http from "k6/http";
import { check } from "k6";
import { URL, buildOptions, handleSummary, record } from "./common.js";

export const options = buildOptions();
export { handleSummary };

const HOOK = __ENV.RC_HOOK_PATH;

export default function () {
  const body = JSON.stringify({
    text: `[ALERT] k6 webhook storm ${__VU}-${__ITER}: service xyz CPU above threshold`,
  });
  const res = record(http.post(`${URL}${HOOK}`, body, { headers: { "Content-Type": "application/json" } }));
  check(res, { "webhook 200": (r) => r.status === 200 });
}
