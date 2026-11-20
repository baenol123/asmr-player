"""주사위 놀이(보드게임)형 작품의 놀이판 정의를 만든다.

- 판 정의 형식은 static/boards/*.json 과 같다 (손으로 만든 판이 있으면 그쪽이 먼저).
- 손으로 만든 판이 없으면 작품의 지도 이미지를 OpenAI 에 보내 판 정의를 만들게 하고,
  칸·연결·트랙이 맞는지 검사해 cache/boards/ 에 저장한다. 검사를 통과하지 못하면
  트랙 번호를 한 줄로 잇는 기본 판을 쓴다.
"""
import base64
import json
import re
from pathlib import PurePosixPath

import requests

import subgen

# 제목·장르·파일 이름에 이런 말이 있으면 보드게임형 작품으로 본다
BOARD_WORDS = re.compile(r"すごろく|双六|スゴロク|サイコロ|ボードゲーム|주사위|보드게임|骰子|大富翁|sugoroku|board ?game", re.I)
MAP_WORDS = re.compile(r"地図|地圖|マップ|map|지도|盤|board|すごろく|双六|スゴロク|주사위", re.I)
RULE_WORDS = re.compile(r"ルール|rule|遊び方|規則|說明|説明|설명|규칙|노는 ?법|방법", re.I)
KINDS = ["start", "goal", "normal", "trap", "rest", "stop", "special"]
MIN_TILES = 6
MAX_IMAGE_BYTES = 15 * 2**20


def tile_number(path):
    """'음성/08s.wav' → 8. 파일 이름이 숫자로 시작하지 않으면 None"""
    m = re.match(r"0*(\d+)", PurePosixPath(path).name)
    return int(m.group(1)) if m else None


def track_numbers(tracks):
    out = {}
    for t in tracks:
        n = tile_number(t["path"])
        if n is not None and n not in out:
            out[n] = t["path"]
    return out


def looks_like_board(meta, tracks, others):
    """보드게임형 작품인지. 번호로 시작하는 트랙이 충분하고, 제목·장르·파일 이름에 주사위 놀이를 뜻하는 말이 있어야 한다"""
    if len(track_numbers(tracks)) < MIN_TILES:
        return False
    meta = meta or {}
    text = " ".join([str(meta.get("title") or ""), str(meta.get("title_orig") or ""), *(meta.get("genres") or [])])
    names = " ".join(o["name"] for o in others)
    return bool(BOARD_WORDS.search(text) or BOARD_WORDS.search(names))


def pick_images(others):
    """(지도 이미지, 규칙 이미지) 경로. 없으면 None"""
    images = [o for o in others if o["kind"] == "image"]

    def rank(o, words):
        return (0 if words.search(o["name"]) else 1, o["path"].count("/"), o["path"])
    maps = sorted((o for o in images if MAP_WORDS.search(o["name"])), key=lambda o: rank(o, MAP_WORDS))
    rules = sorted((o for o in images if RULE_WORDS.search(o["name"]) and not MAP_WORDS.search(o["name"])),
                   key=lambda o: rank(o, RULE_WORDS))
    return (maps[0]["path"] if maps else None), (rules[0]["path"] if rules else None)


def basic_board(tracks, title):
    """트랙 번호를 지그재그로 한 줄로 잇는 판. 지도를 읽지 못했을 때 쓴다"""
    nums = sorted(track_numbers(tracks))
    cols = 5
    tiles = []
    for i, n in enumerate(nums):
        r, k = divmod(i, cols)
        c = k if r % 2 == 0 else cols - 1 - k
        kind = "start" if i == 0 else "goal" if i == len(nums) - 1 else "normal"
        tiles.append({"n": n, "c": c, "r": r, "kind": kind,
                      "label": "START" if kind == "start" else "GOAL" if kind == "goal" else None})
    edges = [{"from": a, "to": b} for a, b in zip(nums, nums[1:])]
    return {
        "title": "주사위 놀이판", "subtitle": title, "cols": cols, "rows": (len(nums) + cols - 1) // cols,
        "start": nums[0], "goal": nums[-1], "stops": [], "exactGoal": False,
        "dice": [{"key": "normal", "label": "기본", "faces": 6}],
        "legend": [{"kind": "normal", "label": "일반 칸"}],
        "rules": ["주사위를 굴려 나온 눈만큼 나아가고, 멈춘 칸의 트랙을 듣습니다.",
                  "지도를 읽지 못해 칸을 번호 순서대로 이은 기본 판이에요. 갈림길이나 특수 칸은 원본 지도를 보고 따라 주세요."],
        "tiles": tiles, "edges": edges,
    }


# ── AI 로 지도 읽기 ───────────────────────────────────────────────────
_NULL_STR = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "subtitle", "start", "goal", "stops", "tolls", "exactGoal", "dice", "legend", "rules", "tiles", "edges"],
    "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "start": {"type": "integer"},
        "goal": {"type": "integer"},
        "stops": {"type": "array", "items": {"type": "integer"}},
        "tolls": {"type": "array", "items": {"type": "integer"}},
        "exactGoal": {"type": "boolean"},
        "dice": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["key", "label", "faces", "minus"],
            "properties": {"key": {"type": "string"}, "label": {"type": "string"}, "faces": {"type": "integer"},
                           "minus": {"type": "integer"}}}},
        "legend": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["kind", "label"],
            "properties": {"kind": {"type": "string", "enum": KINDS}, "label": {"type": "string"}}}},
        "rules": {"type": "array", "items": {"type": "string"}},
        "tiles": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["n", "c", "r", "kind", "label"],
            "properties": {"n": {"type": "integer"}, "c": {"type": "integer"}, "r": {"type": "integer"},
                           "kind": {"type": "string", "enum": KINDS}, "label": _NULL_STR}}},
        "edges": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["from", "to", "via", "label", "both"],
            "properties": {
                "from": {"type": "integer"}, "to": {"type": "integer"},
                "via": {"type": ["array", "null"], "items": {
                    "type": "object", "additionalProperties": False, "required": ["c", "r"],
                    "properties": {"c": {"type": "integer"}, "r": {"type": "integer"}}}},
                "label": _NULL_STR, "both": {"type": "boolean"}}}},
    },
}

PROMPT = """You convert the map of a sugoroku (dice board game) from a Japanese audio work into JSON for a player app.
The first image is the board map. A second image, if present, is the rule sheet.

Audio files of this work (each square's file name starts with the square number, e.g. "08s" is square 8):
{files}
Work title: {title}

Output rules:
- One tile per square on the map, with n = the square number. START and GOAL squares also get their numbers
  (START is usually 0; GOAL is usually the last number, the one whose file means goal/ending).
- Place tiles on an integer grid that copies the map layout: c = column from the left (0-based), r = row from the
  top (0-based). Keep neighbours that the map draws side by side in adjacent cells so connecting lines run
  horizontally or vertically. Two tiles never share a cell.
- kind: start, goal, normal, trap, rest, stop (a forced stop: the piece must stop there even with steps left),
  special (any other special square). label: the text printed on the square in Korean (keep START, GOAL, TRAP and
  STOP in English as printed); null for a plain numbered square. Keep labels short (at most 5 Korean characters).
- edges: every connection a piece can move along, in the forward direction. both=true when the rules allow moving
  either way on that connection. via: the grid cells a bent line passes through (where it turns), null for a straight
  connection between two tiles in the same row or column. label: text printed on the line (e.g. "ショートカット" →
  "지름길"), else null.
- stops: numbers of forced-stop squares. exactGoal: true if the goal needs an exact roll.
- tolls: squares whose track must be heard even when a move only passes over them, then the piece keeps moving
  (e.g. the square on a shortcut route when the map says choosing that route brings a strict command). Usually [].
- dice: difficulty options if the rules describe them (key, Korean label, number of faces of the die actually rolled,
  minus = the number subtracted from the roll, a result below 1 counting as 1; 0 if nothing is subtracted).
  "Minus 2" on a normal die is faces 6, minus 2. Otherwise [{{"key": "normal", "label": "기본", "faces": 6, "minus": 0}}].
- legend: the square kinds that appear, with their Korean names as the map's legend calls them.
- rules: the map's and rule sheet's instructions in natural Korean, one short sentence each. The app plays the
  square's track by itself, so say "멈춘 칸의 트랙을 듣습니다" instead of telling the player to play a file.
- title: the map's title in Korean. subtitle: the work title in Korean.
"""


def _image_part(path):
    data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"이미지가 너무 큽니다: {path.name}")
    mime = {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(path.suffix.lower(), "image/jpeg")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}",
                                               "detail": "high"}}


def ask_ai(map_path, rule_path, tracks, title):
    key = subgen.openai_key()
    if not key:
        raise ValueError("OpenAI 키가 없습니다 (openai_key.txt)")
    files = "\n".join(PurePosixPath(t["path"]).name for t in tracks)
    content = [{"type": "text", "text": PROMPT.format(files=files, title=title)}, _image_part(map_path)]
    if rule_path:
        content.append(_image_part(rule_path))
    schema_ok = True
    while True:
        body = {
            "model": subgen.OPENAI_MODEL,
            "messages": [{"role": "user", "content": content}],
            "reasoning_effort": "medium",   # 칸 배치를 옮기는 데는 생각이 필요하다
            "response_format": {"type": "json_schema", "json_schema": {"name": "board", "strict": True, "schema": SCHEMA}}
            if schema_ok else {"type": "json_object"},
        }
        r = requests.post(subgen.OPENAI_URL, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=(15, 600))
        if r.status_code == 400 and schema_ok and re.search(r"json_schema|response_format", r.text, re.I):
            schema_ok = False
            continue
        if r.status_code >= 400:
            raise ValueError(f"OpenAI 오류 {r.status_code}: {r.text[:300]}")
        message = r.json()["choices"][0].get("message") or {}
        if message.get("refusal"):
            raise ValueError("모델이 지도 읽기를 거부했습니다")
        return json.loads(message.get("content") or "")


def _straighten(a, via, b, occupied):
    """선은 가로·세로로만 그린다. 대각선 구간에는 꺾이는 점을 넣는다 (가능하면 칸이 없는 자리로)"""
    pts, out = [list(a), *via, list(b)], []
    for p, q in zip(pts, pts[1:]):
        if p[0] != q[0] and p[1] != q[1]:
            elbow = (q[0], p[1])
            if elbow in occupied and (p[0], q[1]) not in occupied:
                elbow = (p[0], q[1])
            out.append(list(elbow))
        out.append(q)
    return out[:-1]


def validate(d, tracks):
    """AI 가 만든 판을 검사하고 앱이 쓰는 형식으로 다듬는다. 문제가 있으면 ValueError"""
    have = track_numbers(tracks)
    tiles, seen_n, seen_cell = [], set(), set()
    for t in d.get("tiles") or []:
        n, c, r = int(t["n"]), int(t["c"]), int(t["r"])
        if n in seen_n:
            raise ValueError(f"{n}칸이 두 번 나옵니다")
        if c < 0 or r < 0 or (c, r) in seen_cell:
            raise ValueError(f"{n}칸의 자리가 다른 칸과 겹칩니다")
        seen_n.add(n)
        seen_cell.add((c, r))
        kind = t.get("kind") if t.get("kind") in KINDS else "normal"
        tiles.append({"n": n, "c": c, "r": r, "kind": kind, "label": t.get("label") or None})
    if len(tiles) < MIN_TILES:
        raise ValueError("칸이 너무 적습니다")
    start, goal = int(d["start"]), int(d["goal"])
    if start not in seen_n or goal not in seen_n:
        raise ValueError("출발 칸이나 골 칸이 판에 없습니다")
    missing = sorted(n for n in seen_n if n not in have)
    if missing:
        raise ValueError("트랙이 없는 칸: " + ", ".join(map(str, missing[:10])))
    cols = max(t["c"] for t in tiles) + 1
    rows = max(t["r"] for t in tiles) + 1
    cell = {t["n"]: (t["c"], t["r"]) for t in tiles}
    edges, adj = [], {}
    for e in d.get("edges") or []:
        a, b = int(e["from"]), int(e["to"])
        if a not in seen_n or b not in seen_n or a == b:
            raise ValueError(f"{a}→{b} 연결이 없는 칸을 가리킵니다")
        via = [[min(max(int(p["c"]), 0), cols - 1), min(max(int(p["r"]), 0), rows - 1)] for p in (e.get("via") or [])]
        via = _straighten(cell[a], via, cell[b], seen_cell)
        edge = {"from": a, "to": b}
        if via:
            edge["via"] = via
        if e.get("label"):
            edge["label"] = str(e["label"])[:8]
        if e.get("both"):
            edge["both"] = True
        edges.append(edge)
        adj.setdefault(a, set()).add(b)
        if e.get("both"):
            adj.setdefault(b, set()).add(a)
    # 출발 칸에서 모든 칸, 특히 골까지 갈 수 있어야 한다
    reach, todo = {start}, [start]
    while todo:
        for nx in adj.get(todo.pop(), ()):
            if nx not in reach:
                reach.add(nx)
                todo.append(nx)
    if goal not in reach:
        raise ValueError("출발 칸에서 골까지 이어지지 않습니다")
    cut = sorted(seen_n - reach)
    if cut:
        raise ValueError("출발 칸에서 갈 수 없는 칸: " + ", ".join(map(str, cut[:10])))
    dice = [{"key": str(x["key"]) or f"d{i}", "label": str(x["label"]), "faces": int(x["faces"]),
             **({"minus": int(x["minus"])} if 0 < int(x.get("minus") or 0) < int(x["faces"]) else {})}
            for i, x in enumerate(d.get("dice") or []) if 1 <= int(x["faces"]) <= 20]
    return {
        "title": str(d.get("title") or "주사위 놀이판"), "subtitle": str(d.get("subtitle") or ""),
        "cols": cols, "rows": rows, "start": start, "goal": goal,
        "stops": [int(n) for n in d.get("stops") or [] if int(n) in seen_n],
        "tolls": list(dict.fromkeys(int(n) for n in d.get("tolls") or [] if int(n) in seen_n - {start, goal})),
        "exactGoal": bool(d.get("exactGoal")),
        "dice": dice or [{"key": "normal", "label": "기본", "faces": 6}],
        "legend": [{"kind": x["kind"], "label": str(x["label"])} for x in d.get("legend") or [] if x.get("kind") in KINDS],
        "rules": [str(x) for x in d.get("rules") or []][:12],
        "tiles": tiles, "edges": edges,
    }


def generate(map_path, rule_path, tracks, title):
    """지도를 읽어 검사까지 마친 판 정의. 실패하면 ValueError / requests 예외"""
    return validate(ask_ai(map_path, rule_path, tracks, title), tracks)
