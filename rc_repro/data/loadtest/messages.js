// Post messages via chat.postMessage — the write-path throughput scenario.
import http from "k6/http";
import { check } from "k6";
import { URL, authParams, buildOptions, handleSummary, record } from "./common.js";

export const options = buildOptions();
export { handleSummary };

export default function () {
  const body = JSON.stringify({ channel: "#general", text: `k6 load ${__VU}-${__ITER}` });
  const res = record(http.post(`${URL}/api/v1/chat.postMessage`, body, authParams));
  check(res, { "postMessage 200": (r) => r.status === 200 });
}
