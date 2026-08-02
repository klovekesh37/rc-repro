// A badly-written bot/integration: tight polling loops with no pagination and
// no backoff — the "customer's custom script is hammering us" ticket. Each
// iteration polls history (large count), lists users unpaginated, and lists
// channels, back to back.
import http from "k6/http";
import { check } from "k6";
import { URL, buildOptions, handleSummary, record, vuAuth } from "./common.js";

export const options = buildOptions();
export { handleSummary };

export default function () {
  const auth = vuAuth();
  // Check all three: checks_rate described only 1/3 of the traffic, and the two
  // unchecked calls are the unpaginated ones this scenario exists to stress.
  const hist = record(http.get(`${URL}/api/v1/channels.history?roomName=general&count=100`, auth));
  const users = record(http.get(`${URL}/api/v1/users.list`, auth));
  const chans = record(http.get(`${URL}/api/v1/channels.list`, auth));
  check(hist, { "poll 200": (r) => r.status === 200 });
  check(users, { "users.list 200": (r) => r.status === 200 });
  check(chans, { "channels.list 200": (r) => r.status === 200 });
}
