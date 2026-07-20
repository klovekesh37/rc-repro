"""Data-scale prefill for `seed --scale` — bulk MongoDB inserts.

The REST seed authors realistic, app-consistent content but is far too slow for
scale repros ("50k users", "a room with 800k messages won't load"): each message
is a ~10-40ms HTTP round-trip. This path writes documents straight to MongoDB
via mongosh `insertMany` (tens of thousands/sec).

TRADE-OFF, stated plainly: this BYPASSES Rocket.Chat's application hooks. The
documents are minimal-but-readable — enough that RC lists/loads them (directory,
admin user list, room history, search) — but bulk users have NO login
credentials and messages fire no notifications/mentions/threading side-effects.
Use it to reproduce SCALE and PERFORMANCE behaviour, not feature behaviour; use
the REST seed (default) when you need real, loginable users.

Everything runs through `docker compose exec mongodb` (mongosh, legacy `mongo`
fallback), mirroring perf/mongoprof.py.
"""

from __future__ import annotations

import re

from rc_repro import runner

DB = "rocketchat"
_URI = f"mongodb://localhost:27017/{DB}"
_BATCH = 5000                    # docs per insertMany — well under the 16MB BSON cap
_SCALE_TAG = "rcrepro_scale"     # stamped on every doc so `seed --scale --clear` can undo it

# NOTE: RC prefixes most collections (rocketchat_room, rocketchat_message) but
# the users collection is plain `users` — the JS below reflects that.

_SPEC_RE = re.compile(r"^(users|messages)=(\d+)(?:@([\w-]+))?$")


def parse_scale(spec: str) -> dict:
    """'users=50000,messages=800000@team-chat' -> {'users': 50000,
    'messages': (800000, 'team-chat')}. Raises ValueError on bad input."""
    out: dict = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = _SPEC_RE.match(part)
        if not m:
            raise ValueError(
                f"bad --scale term {part!r} (want users=N or messages=N@room)")
        kind, n, room = m.group(1), int(m.group(2)), m.group(3)
        if kind == "users":
            out["users"] = n
        else:
            if not room:
                raise ValueError("messages=N needs a target room: messages=N@room")
            out["messages"] = (n, room)
    return out


def _eval(name: str, js: str) -> tuple[int, str]:
    # Wrap the body so a JS/Mongo error (e.g. a BulkWriteError) is printed as
    # {error} on STDOUT and the shell still exits 0. compose_exec_capture drops
    # stderr, so without this a JS error would look like "empty output" — and
    # worse, the non-zero exit would trigger the `mongo` fallback to RE-RUN the
    # (non-idempotent) insert. The fallback is now reserved for a genuinely
    # missing mongosh binary (the shell fails to start -> rc != 0, no output).
    wrapped = f"try {{\n{js}\n}} catch (e) {{ print(JSON.stringify({{error: '' + e}})); }}"
    out = ""
    for shell in ("mongosh", "mongo"):
        rc, out = runner.compose_exec_capture(
            name, "mongodb", [shell, "--quiet", _URI, "--eval", wrapped])
        if rc == 0:
            return rc, out
    return 1, out


def bulk_users(name: str, count: int, *, batch: int = _BATCH) -> tuple[int, str]:
    """Insert `count` minimal users (scaleuserN). No password — not loginable.
    mongosh builds each batch itself, so the --eval string stays tiny."""
    js = f"""
    var TOTAL = {int(count)}, BATCH = {int(batch)}, made = 0;
    var start = db['users'].countDocuments({{'{_SCALE_TAG}': true}});
    for (var off = 0; off < TOTAL; off += BATCH) {{
      var docs = [];
      for (var i = off; i < Math.min(off + BATCH, TOTAL); i++) {{
        var n = start + i + 1;
        docs.push({{
          createdAt: new Date(), _updatedAt: new Date(),
          username: 'scaleuser' + n, name: 'Scale User ' + n,
          emails: [{{address: 'scaleuser' + n + '@scale.example', verified: true}}],
          type: 'user', status: 'offline', active: true, roles: ['user'],
          '{_SCALE_TAG}': true
        }});
      }}
      db['users'].insertMany(docs, {{ordered: false}});
      made += docs.length;
    }}
    print(JSON.stringify({{inserted: made}}));
    """
    return _eval(name, js)


def bulk_messages(name: str, count: int, room: str, *, batch: int = _BATCH) -> tuple[int, str]:
    """Insert `count` messages into `room` (by name or _id), then fix the room's
    msgs counter and lastMessage so RC's UI stays consistent."""
    js = f"""
    var room = db['rocketchat_room'].findOne({{$or: [{{name: {room!r}}}, {{_id: {room!r}}}]}});
    if (!room) {{ print(JSON.stringify({{error: 'room not found: ' + {room!r}}})); }} else {{
      var TOTAL = {int(count)}, BATCH = {int(batch)}, made = 0;
      var author = {{_id: 'rocket.cat', username: 'rocket.cat', name: 'Rocket.Cat'}};
      var last = null;
      for (var off = 0; off < TOTAL; off += BATCH) {{
        var docs = [];
        for (var i = off; i < Math.min(off + BATCH, TOTAL); i++) {{
          last = {{
            rid: room._id, msg: 'scale message ' + (i + 1),
            ts: new Date(), _updatedAt: new Date(), u: author,
            '{_SCALE_TAG}': true
          }};
          docs.push(last);
        }}
        var r = db['rocketchat_message'].insertMany(docs, {{ordered: false}});
        made += docs.length;
      }}
      if (made > 0) {{
        db['rocketchat_room'].updateOne({{_id: room._id}}, {{$inc: {{msgs: made}},
          $set: {{lm: new Date(), lastMessage: last}}}});
      }}
      print(JSON.stringify({{inserted: made, room: room._id}}));
    }}
    """
    return _eval(name, js)


def clear(name: str) -> tuple[int, str]:
    """Remove everything this module inserted (matched by the scale tag), and put
    each affected room back to a consistent state: decrement its msgs counter and
    reset lastMessage/lm to the newest REMAINING message (or clear them if the
    room is now empty) — otherwise the room would point at a deleted message."""
    js = f"""
    var perRoom = db['rocketchat_message'].aggregate([
      {{$match: {{'{_SCALE_TAG}': true}}}},
      {{$group: {{_id: '$rid', n: {{$sum: 1}}}}}}
    ]).toArray();
    var u = db['users'].deleteMany({{'{_SCALE_TAG}': true}}).deletedCount;
    var m = db['rocketchat_message'].deleteMany({{'{_SCALE_TAG}': true}}).deletedCount;
    perRoom.forEach(function (g) {{
      var newest = db['rocketchat_message'].find({{rid: g._id}}).sort({{ts: -1}}).limit(1).toArray()[0];
      var upd = {{$inc: {{msgs: -g.n}}}};
      if (newest) {{ upd.$set = {{lastMessage: newest, lm: newest.ts}}; }}
      else {{ upd.$unset = {{lastMessage: '', lm: ''}}; }}
      db['rocketchat_room'].updateOne({{_id: g._id}}, upd);
    }});
    print(JSON.stringify({{users: u, messages: m, rooms: perRoom.length}}));
    """
    return _eval(name, js)
