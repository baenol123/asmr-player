"""DLsite 작품 추천.

소장작의 태그·서클·성우로 취향을 계산하고, DLsite 검색 결과를 사용자가 고른 조건으로
걸러 점수순으로 돌려준다. 소장작과 '들어봄' 으로 표시한 작품은 뺀다.

"""
import html
import json
import logging
import math
import re
import threading
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Blueprint, jsonify, request, send_from_directory

bp = Blueprint("recommend", __name__)
log = logging.getLogger(__name__)

SEARCH = ("https://www.dlsite.com/maniax/fsr/ajax/=/language/jp/sex_category%5B0%5D/male"
         "/work_category%5B0%5D/doujin/work_type_category%5B0%5D/audio/per_page/100/show_type/1")
INFO = "https://www.dlsite.com/maniax/product/info/ajax?product_id={}&cdn_cache_min=1"
WORK_URL = "https://www.dlsite.com/maniax/work/=/product_id/{}.html"
ORDERS = {"dl": "dl_d", "new": "release_d", "old": "release"}
CACHE_SECONDS = 30 * 60
CACHE_MAX = 5000          # 캐시 항목이 이보다 많아지면 만료된 것부터 지운다
LIBRARY_SECONDS = 60      # 소장작 스캔 결과를 재사용하는 시간
MAX_PAGES = 5
MAX_LIMIT = 300
REQUEST_GAP = 0.5         # 캐시에 없는 DLsite 요청 사이 최소 간격(초)
GENRE_LIST = "https://www.dlsite.com/maniax/genre/list"
GENRE_LIST_SECONDS = 24 * 3600


def _hit(rx, text):
    return bool(rx and text and rx.search(text))


_paths = {}
_session = None
_session_lock = threading.Lock()
_lock = threading.Lock()          # 저장 파일(들어봄·장르 번호) 읽고 쓰기
_cache_lock = threading.Lock()    # 메모리 캐시
_net_lock = threading.Lock()      # 요청 간격 맞추기
_last_request = 0.0
_page_cache = {}   # url -> (time, rows)
_info_cache = {}   # id -> (time, info)    실패한 요청은 넣지 않는다
_library = (0.0, None)
_MISS = object()


def init(works_dir, meta_dir, cache_dir):
    _paths.update(works=Path(works_dir), meta=Path(meta_dir), cache=Path(cache_dir))


def session():
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                       "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
            s.cookies.set("adultchecked", "1", domain=".dlsite.com")
            s.cookies.set("locale", "ko-kr", domain=".dlsite.com")
            _session = s
    return _session


def _get(url):
    """DLsite 요청. 연달아 보낼 때는 REQUEST_GAP 만큼 간격을 둔다."""
    global _last_request
    with _net_lock:
        wait = _last_request + REQUEST_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
    r = session().get(url, timeout=30)
    r.raise_for_status()
    return r


# ── 메모리 캐시 ──────────────────────────────────────────────────────
def _cache_get(cache, key):
    with _cache_lock:
        hit = cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    return _MISS


def _cache_put(cache, key, value):
    now = time.time()
    with _cache_lock:
        cache[key] = (now, value)
        if len(cache) > CACHE_MAX:
            for k in [k for k, (t, _) in cache.items() if now - t >= CACHE_SECONDS]:
                del cache[k]


# ── 입력값 다듬기 ────────────────────────────────────────────────────
def _int(v, default, lo=None, hi=None):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _tag_list(v):
    if isinstance(v, str):
        v = [v]
    out = []
    for t in v or []:
        t = t.strip() if isinstance(t, str) else ""
        if t and t not in out:
            out.append(t)
    return out


# ── 저장 파일 ────────────────────────────────────────────────────────
def _json_file(name, default):
    f = _paths["cache"] / name
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(name, data):
    f = _paths["cache"] / name
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(f)


def seen_list():
    return _json_file("recommend_seen.json", {})


def genre_ids():
    return _json_file("dlsite_genres.json", {})


def _learn_genres(learned):
    with _lock:
        known = genre_ids()
        if any(known.get(k) != v for k, v in learned.items()):
            known.update(learned)
            _save_json("dlsite_genres.json", known)


_genre_list_at = 0.0


def ensure_genre_list():
    """DLsite 전체 태그 목록(이름 -> 번호)을 하루 한 번 받아 둔다.
    소장작 정보에만 있는 옛 태그 이름(DLsite 에서 이름이 바뀐 것)은 여기에 없다."""
    global _genre_list_at
    f = _paths["cache"] / "dlsite_genres.json"
    try:
        fresh = time.time() - f.stat().st_mtime < GENRE_LIST_SECONDS
    except OSError:
        fresh = False
    if _genre_list_at and (fresh or time.time() - _genre_list_at < GENRE_LIST_SECONDS):
        return
    try:
        page = _get(GENRE_LIST).text
    except requests.RequestException as e:
        log.warning("recommend: 태그 목록을 받지 못함: %s", e)
        return
    found = {_text(n): g for g, n in re.findall(r'/genre/=/genre/(\d+)"[^>]*>([^<]+)<span', page)}
    if found:
        _learn_genres(found)
        _genre_list_at = time.time()


# ── 소장작과 취향 ────────────────────────────────────────────────────
def owned_ids():
    ids = set()
    try:
        entries = list(_paths["works"].iterdir())
    except OSError as e:
        log.warning("recommend: 작품 폴더를 읽지 못함: %s", e)
        return ids
    for p in entries:
        m = re.search(r"[RBV]J\d{6,8}", p.name, re.I)
        if m:
            ids.add(m.group(0).upper())
    return ids


def _scan_library():
    """소장작 메타에서 태그·서클·성우 빈도를 센다."""
    owned = owned_ids()
    tags, circles, cvs = Counter(), Counter(), Counter()
    n = missing = 0
    for wid in owned:
        f = _paths["meta"] / f"{wid}.json"
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            missing += 1
            continue
        if m.get("notfound"):
            missing += 1
            continue
        n += 1
        tags.update(set(m.get("genres") or []))
        if m.get("circle"):
            circles[m["circle"]] += 1
        cvs.update(set(m.get("cvs") or []))
    return {"count": n, "missing": missing, "owned": owned,
            "tags": tags, "circles": circles, "cvs": cvs}


def library(fresh=False):
    """소장작 스캔은 매 요청마다 할 필요가 없어 잠깐 재사용한다."""
    global _library
    t, lib = _library
    if fresh or lib is None or time.time() - t >= LIBRARY_SECONDS:
        lib = _scan_library()
        _library = (time.time(), lib)
    return lib


# ── DLsite 검색 결과 읽기 ────────────────────────────────────────────
ROW_ID = re.compile(r'data-list_item_product_id="([A-Z]{2}\d+)"')


def _text(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse_rows(page):
    # 한 작품 안에 id 속성이 여러 번 나와도 정보가 잘리지 않도록 같은 id 조각을 이어 붙인다
    bodies = {}
    parts = ROW_ID.split(page)
    for i in range(1, len(parts) - 1, 2):
        bodies[parts[i]] = bodies.get(parts[i], "") + parts[i + 1]

    rows, learned = [], {}
    for rid, body in bodies.items():
        title = re.search(r'<dt class="work_name">.*?<a [^>]*title="([^"]*)"', body, re.S)
        maker = re.search(r'<dd class="maker_name">(.*?)</dd>', body, re.S)
        circle, cvs = None, []
        if maker:
            circle_m = re.search(r"<a [^>]*>(.*?)</a>", maker.group(1), re.S)
            circle = _text(circle_m.group(1)) if circle_m else None
            author = re.search(r'<span class="author">(.*?)</span>', maker.group(1), re.S)
            if author:
                cvs = [_text(a) for a in re.findall(r"<a [^>]*>(.*?)</a>", author.group(1), re.S)]
        tags = []
        tag_block = re.search(r'<dd class="search_tag">(.*?)</dd>', body, re.S)
        if tag_block:
            for gid, name in re.findall(r'href="[^"]*/genre/(\d+)/[^"]*"[^>]*>(.*?)</a>', tag_block.group(1), re.S):
                name = _text(name)
                if name and name not in tags:
                    tags.append(name)
                    learned[name] = gid
        # 목록에는 태그가 일부만 보인다. 숨은 속성값에 전체 장르 번호가 있다
        attrs = re.search(r'__product_attributes"[^>]*?value="([^"]*)"', body)
        attr_list = attrs.group(1).split(",") if attrs else []
        gids = [g for g in attr_list if g.isdigit()]
        price = re.search(r'data-price="(\d+)"', body)
        date = re.search(r'class="sales_date">[^\d]*(\d{4})\D+(\d{1,2})\D+(\d{1,2})', body)
        dl = re.search(r'_dl_count_[A-Z0-9]+">([\d,]+)<', body)
        thumb = re.search(r"'(//img\.dlsite\.jp/[^']+_img_main\.jpg)'", body)
        rows.append({
            "id": rid,
            "title": html.unescape(title.group(1)) if title else rid,
            "circle": circle, "cvs": cvs, "tags": tags, "gids": gids,
            "price": int(price.group(1)) if price else None,
            "date": "%s-%02d-%02d" % (date.group(1), int(date.group(2)), int(date.group(3))) if date else "",
            "dl": int(dl.group(1).replace(",", "")) if dl else 0,
            "adult": ("adl" in attr_list) if attrs else ("icon_GEN" not in body),
            "thumb": "https:" + thumb.group(1) if thumb else None,
        })
    return rows, learned


def fetch_page(url):
    hit = _cache_get(_page_cache, url)
    if hit is not _MISS:
        return hit
    data = _get(url).json()
    page = data.get("search_result") if isinstance(data, dict) else None
    rows, learned = parse_rows(page or "")
    if learned:
        _learn_genres(learned)
    _cache_put(_page_cache, url, rows)
    return rows


def fetch_info(ids):
    """평점·판매 수. 돌려주는 값: ({id: info}, 실패한 id 집합). 실패는 캐시하지 않는다."""
    out, need, failed = {}, [], set()
    for i in ids:
        hit = _cache_get(_info_cache, i)
        if hit is _MISS:
            need.append(i)
        else:
            out[i] = hit
    for k in range(0, len(need), 100):
        chunk = need[k:k + 100]
        try:
            data = _get(INFO.format(",".join(chunk))).json()
        except (requests.RequestException, ValueError) as e:
            log.warning("recommend: 작품 정보 요청 실패 (%d개): %s", len(chunk), e)
            failed.update(chunk)
            for i in chunk:
                out[i] = {}
            continue
        if not isinstance(data, dict):  # 결과가 없으면 빈 배열이 온다
            data = {}
        for i in chunk:
            info = data.get(i) or {}
            _cache_put(_info_cache, i, info)
            out[i] = info
    return out, failed


def search_url(include_ids, keyword, order, page, any_tag=False):
    path = SEARCH
    for n, gid in enumerate(include_ids):
        path += f"/genre%5B{n}%5D/{gid}"
    if include_ids:
        path += "/genre_and_or/" + ("or" if any_tag else "and")
    if keyword:
        path += "/keyword/" + quote(keyword, safe="")
    path += f"/order%5B0%5D/{ORDERS.get(order, 'dl_d')}"
    if page > 1:
        path += f"/page/{page}"
    return path


# ── 추천 ─────────────────────────────────────────────────────────────
def full_tags(work, names):
    """보이는 태그 + 숨은 장르 번호를 이름으로 바꾼 것."""
    tags = list(work["tags"])
    for g in work["gids"]:
        n = names.get(g)
        if n and n not in tags:
            tags.append(n)
    return tags


def has_tag(work, tag, known):
    # 숨은 장르 번호를 못 읽은 작품도 있으니 보이는 태그도 함께 본다
    gid = known.get(tag)
    return tag in work["tags"] or (gid is not None and gid in work["gids"])


def taste_score(work, lib):
    """취향 점수 = 태그 점수 + 서클 점수 + 성우 점수.

    태그: 작품의 태그마다 '그 태그가 붙은 소장작 비율 × 10' 을 더한다.
    서클: 소장작이 있는 서클이면 2 + log2(소장작 수).
    성우: 소장작이 있는 성우 중 가장 많은 한 명만 1.5 + log2(소장작 수).
    돌려주는 값: (합계, 이유 문장 목록, {"tag", "circle", "cv"} 항목별 점수)
    """
    n = max(lib["count"], 1)
    reasons = []
    parts = {"tag": 0.0, "circle": 0.0, "cv": 0.0}
    hits = []
    for t in work["tags"]:
        share = lib["tags"].get(t, 0) / n
        if share:
            parts["tag"] += share * 10
            hits.append((share, t))
    hits.sort(reverse=True)
    if hits:
        reasons.append("취향 태그: " + ", ".join(t for _, t in hits[:4]))
    c = lib["circles"].get(work["circle"] or "", 0)
    if c:
        parts["circle"] = 2 + math.log2(c)
        reasons.append(f"소장작 {c}개 서클")
    cv_hits = [(lib["cvs"][v], v) for v in work["cvs"] if lib["cvs"].get(v)]
    if cv_hits:
        best = max(cv_hits)
        parts["cv"] = 1.5 + math.log2(best[0])
        reasons.append(f"소장작 {best[0]}개 성우 {best[1]}")
    parts = {k: round(v, 1) for k, v in parts.items()}
    return round(sum(parts.values()), 1), reasons, parts


def _health_warnings(pool):
    """파싱이 대부분 실패했으면 DLsite 화면 구조가 바뀐 것일 수 있다."""
    works = list(pool.values())
    if len(works) < 10:
        return []
    out = []
    if sum(w["title"] == w["id"] for w in works) > len(works) / 2:
        out.append("작품 제목을 거의 읽지 못했습니다. DLsite 페이지 구조가 바뀌었을 수 있습니다.")
    if sum(not w["tags"] and not w["gids"] for w in works) > len(works) / 2:
        out.append("작품 태그를 거의 읽지 못했습니다. 태그 조건과 취향 점수가 부정확할 수 있습니다.")
    return out


def recommend(opts):
    lib = library()
    seen = seen_list()
    include = _tag_list(opts.get("include"))
    exclude = [t for t in _tag_list(opts.get("exclude")) if t not in include]
    ensure_genre_list()
    known = genre_ids()
    errors = []
    if any(t not in known for t in include + exclude):
        # 처음 보는 태그 이름 — 일반 검색 몇 쪽으로 장르 번호를 익힌다 (결과는 캐시된다)
        for p in range(1, 4):
            try:
                fetch_page(search_url([], "", "dl", p))
            except (requests.RequestException, ValueError) as e:
                errors.append(f"태그 번호 확인 {p}쪽: {e}")
                break
        known = genre_ids()
    names = {g: n for n, g in known.items()}
    include_ids = [known[t] for t in include if t in known]
    any_tag = opts.get("include_mode") == "any"   # 포함 태그를 하나 이상만 가져도 되는지
    pages = _int(opts.get("pages"), 3, 1, MAX_PAGES)
    keyword = (opts.get("keyword") or "").strip() if isinstance(opts.get("keyword"), str) else ""

    pool = {}
    for p in range(1, pages + 1):
        try:
            rows = fetch_page(search_url(include_ids, keyword, opts.get("order"), p, any_tag))
        except (requests.RequestException, ValueError) as e:
            errors.append(f"{p}쪽: {e}")
            break
        for r in rows:
            pool.setdefault(r["id"], r)
        if len(rows) < 100:
            break
    errors += _health_warnings(pool)

    cv_mode = str(opts.get("cv") or "any")
    year_from = _int(opts.get("year_from"), 0)
    year_to = _int(opts.get("year_to"), 9999)
    if year_from > year_to:
        year_from, year_to = year_to, year_from
    price_max = _int(opts.get("price_max"), None, 0)
    counts = Counter()
    picked = []
    hide_owned = opts.get("hide_owned", True)
    for w in pool.values():
        if hide_owned and w["id"] in lib["owned"]:
            counts["소장"] += 1; continue
        if w["id"] in seen and not opts.get("show_seen"):
            counts["들어봄"] += 1; continue
        if opts.get("adult_only", True) and not w["adult"]:
            counts["전연령"] += 1; continue
        # 번호로 검색한 포함 태그는 DLsite 검색 결과를 믿는다. 공식 태그만 보면
        # '자위 서포트' 태그가 생기기 전 작품(옛 '자위' 태그만 있음)처럼 실제로 맞는 작품이 빠진다
        if include and not (any if any_tag else all)(t in known or has_tag(w, t, known) for t in include):
            counts["포함 태그 없음"] += 1; continue
        if any(has_tag(w, t, known) for t in exclude):
            counts["제외 태그"] += 1; continue
        n_cv = len(w["cvs"])
        if (cv_mode == "1" and n_cv != 1) or (cv_mode == "2" and n_cv < 2):
            counts["성우 수"] += 1; continue
        year = int(w["date"][:4]) if w["date"] else 0
        if year and not (year_from <= year <= year_to):
            counts["출시 연도"] += 1; continue
        if price_max is not None and w["price"] is not None and w["price"] > price_max:
            counts["가격"] += 1; continue
        picked.append(w)

    info, failed = fetch_info([w["id"] for w in picked])
    if failed:
        errors.append(f"작품 {len(failed)}개의 평점 정보를 받지 못했습니다.")
    rate_min = _float(opts.get("rate_min"), 0.0) or 0.0
    out = []
    for w in picked:
        i = info.get(w["id"]) or {}
        rate = _float(i.get("rate_average_2dp"))
        if rate_min and rate is None and w["id"] in failed:
            counts["평점 정보 실패"] += 1; continue
        if rate_min and (rate is None or rate < rate_min):
            counts["평점"] += 1; continue
        w = {**w, "tags": full_tags(w, names)}
        score, reasons, score_parts = taste_score(w, lib)
        out.append({**w, "rate": rate, "rate_count": _int(i.get("rate_count"), 0),
                    "dl": _int(i.get("dl_count"), 0) or w["dl"], "score": score, "score_parts": score_parts, "reasons": reasons,
                    "seen": w["id"] in seen, "owned": w["id"] in lib["owned"],
                    "url": WORK_URL.format(w["id"])})

    sort = opts.get("sort") or "taste"
    keys = {
        "taste": lambda w: (-w["score"], -(w["rate"] or 0)),
        "dl": lambda w: -w["dl"],
        "rate": lambda w: (-(w["rate"] or 0), -w["rate_count"]),
        "new": lambda w: w["date"],                 # 날짜 없는 작품은 뒤로 (역순 정렬)
        "old": lambda w: (w["date"] == "", w["date"]),  # 날짜 없는 작품은 뒤로
    }
    if sort not in keys:
        sort = "taste"
    out.sort(key=keys[sort], reverse=sort == "new")
    limit = _int(opts.get("limit"), 60, 1, MAX_LIMIT)
    unknown = [t for t in include + exclude if t not in known]
    return {"works": out[:limit], "pool": len(pool), "matched": len(out),
            "filtered": counts, "errors": errors, "unknown_tags": unknown}


# ── 라우트 ───────────────────────────────────────────────────────────
@bp.route("/recommend")
def page():
    return send_from_directory(Path(__file__).parent / "static", "recommend.html")


@bp.route("/api/recommend/tags")
def tags():
    lib = library(fresh=True)
    ensure_genre_list()
    known = genre_ids()
    mine, old = [], []
    for t, c in lib["tags"].most_common():
        # DLsite 에 없는 옛 태그 이름은 검색할 수 없다 — 버튼 대신 안내로 보여준다
        (mine if t in known else old).append([t, c])
    others = sorted(t for t in known if t not in lib["tags"])
    return jsonify(library=mine, old=old, others=others, count=lib["count"], missing_meta=lib["missing"])


@bp.route("/api/recommend", methods=["POST"])
def run():
    try:
        return jsonify(recommend(request.get_json(silent=True) or {}))
    except Exception as e:   # 페이지에 원인을 보여준다
        log.exception("recommend 실패")
        return jsonify(error=f"{type(e).__name__}: {e}"), 500


@bp.route("/api/recommend/seen", methods=["GET", "POST", "DELETE"])
def seen():
    with _lock:
        data = seen_list()
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            wid = str(body.get("id") or "").strip().upper()
            if re.fullmatch(r"[A-Z]{2}\d{6,8}", wid):
                data[wid] = {"title": str(body.get("title") or "")[:200], "at": time.time()}
                _save_json("recommend_seen.json", data)
        elif request.method == "DELETE":
            wid = request.args.get("id", "").strip().upper()
            if data.pop(wid, None) is not None:
                _save_json("recommend_seen.json", data)
    items = sorted(({"id": k, **v} for k, v in data.items()), key=lambda x: -x.get("at", 0))
    return jsonify(items)