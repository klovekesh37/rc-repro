// Post messages via chat.postMessage — the write-path throughput scenario.
import http from "k6/http";
import { check } from "k6";
import { URL, buildOptions, handleSummary, record, vuAuth } from "./common.js";

export const options = buildOptions();
export { handleSummary };

export default function () {
  const body = JSON.stringify({ channel: "#general", text: `k6 load ${__VU}-${__ITER}` });
  const res = record(http.post(`${URL}/api/v1/chat.postMessage`, body, vuAuth()));
  check(res, { "postMessage 200": (r) => r.status === 200 });
}
