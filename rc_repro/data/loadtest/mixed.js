// Realistic blend: mostly reads, some posts, occasional login.
import http from "k6/http";
import { check } from "k6";
import { URL, authParams, buildOptions, handleSummary, record } from "./common.js";

export const options = buildOptions();
export { handleSummary };

export default function () {
  const roll = Math.random();
  let res;
  if (roll < 0.6) {
    res = http.get(`${URL}/api/v1/channels.history?roomName=general&count=20`, authParams);
  } else if (roll < 0.9) {
    res = http.post(`${URL}/api/v1/chat.postMessage`,
      JSON.stringify({ channel: "#general", text: `k6 mixed ${__VU}-${__ITER}` }), authParams);
  } else {
    res = http.post(`${URL}/api/v1/login`,
      JSON.stringify({ user: __ENV.RC_USER || "admin", password: __ENV.RC_PASS || "admin123" }),
      { headers: { "Content-Type": "application/json" } });
  }
  record(res);
  check(res, { "status 200": (r) => r.status === 200 });
}
