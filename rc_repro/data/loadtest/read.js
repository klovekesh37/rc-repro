// Read channel history — the read-path scenario.
import http from "k6/http";
import { check } from "k6";
import { URL, authParams, buildOptions, handleSummary, record } from "./common.js";

export const options = buildOptions();
export { handleSummary };

export default function () {
  const res = record(http.get(`${URL}/api/v1/channels.history?roomName=general&count=20`, authParams));
  check(res, { "history 200": (r) => r.status === 200 });
}
