// A realistic user session, per-step timed — the "which part is slow?" scenario.
// Each iteration, AS THIS VU'S SEEDED USER (or admin creds if unseeded):
//   login -> rooms (rooms.get) -> open (channels.history) -> post
//   (chat.postMessage) -> sync (subscriptions.get)
// Every step feeds a step_<name> Trend, which handleSummary emits as `steps`
// so rc-repro can show a per-step latency table.
import http from "k6/http";
import { check } from "k6";
import { Trend } from "k6/metrics";
import { URL, buildOptions, handleSummary, record, vuUser } from "./common.js";

export const options = buildOptions();
export { handleSummary };

const T = {
  login: new Trend("step_login", true),
  rooms: new Trend("step_rooms", true),
  open: new Trend("step_open", true),
  post: new Trend("step_post", true),
  sync: new Trend("step_sync", true),
};

const JSON_HDR = { "Content-Type": "application/json" };

function step(name, res) {
  T[name].add(res.timings.duration);
  record(res);
  check(res, { [`${name} ok`]: (r) => r.status === 200 });
  return res;
}

export default function () {
  const u = vuUser();
  const creds = u
    ? { user: u.username, password: u.password }
    : { user: __ENV.RC_USER || "admin", password: __ENV.RC_PASS || "admin123" };

  const login = step("login", http.post(`${URL}/api/v1/login`,
    JSON.stringify(creds), { headers: JSON_HDR }));
  if (login.status !== 200) return;   // no session — count the failure, skip the rest
  // status 200 does not guarantee a JSON body: a front proxy can answer 200 with
  // an HTML error page, and an unchecked body.data deref throws, killing the
  // iteration via dropped_iterations with nothing in the summary to explain it.
  let data = null;
  try { data = login.json("data"); } catch (e) { data = null; }
  if (!data || !data.authToken) return;
  const auth = { headers: {
    "X-Auth-Token": data.authToken,
    "X-User-Id": data.userId,
    "Content-Type": "application/json",
  } };

  step("rooms", http.get(`${URL}/api/v1/rooms.get`, auth));
  step("open", http.get(`${URL}/api/v1/channels.history?roomName=general&count=20`, auth));
  step("post", http.post(`${URL}/api/v1/chat.postMessage`,
    JSON.stringify({ channel: "#general", text: `k6 journey ${__VU}-${__ITER}` }), auth));
  step("sync", http.get(`${URL}/api/v1/subscriptions.get`, auth));
}
