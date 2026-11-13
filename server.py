# -*- coding: utf-8 -*-
"""
DLsite 구매작 로컬 라이브러리 서버 (모바일 웹앱용)

사용법:
  1. 구매·다운로드한 작품을 works\ 아래에 폴더 단위로 넣는다.
     (폴더명에 RJ코드가 있으면 DLsite에서 표지·태그를 자동으로 가져옴)
  2. python server.py 실행
  3. 폰에서 같은 Wi-Fi로 http://<PC IP>:8974 접속

자막: 오디오 파일과 같은 이름의 .srt/.vtt/.txt(SRT 구조) 파일을
아무 하위 폴더에나 두면 자동 매칭. 같은 이름이 여럿이면 srt > vtt > txt 순.
"""
import gzip
import json
import os
import re
import socket
import time
import unicodedata
import zlib
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import subgen

BASE = Path(__file__).resolve().parent
WORKS_DIR = Path(os.environ.get("ASMR_WORKS_DIR", BASE / "works"))
CACHE_DIR = BASE / "cache"
META_DIR = CACHE_DIR / "meta"
COVER_DIR = CACHE_DIR / "covers"
PORT = int(os.environ.get("ASMR_PORT", "8974"))

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".mp4", ".webm"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
SUB_EXTS = {".srt": 0, ".vtt": 1, ".txt": 2}  # 값이 작을수록 우선
COMPRESSIBLE = {"text/html", "text/plain", "application/json", "application/manifest+json"}
HANGUL_CHARS = re.compile(r"[가-힣]")
KANA_CHARS = re.compile(r"[ぁ-ヿ]")
HAN_CHARS = re.compile(r"[一-鿿]")
RJ_RE = re.compile(r"RJ\d{6,8}", re.IGNORECASE)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"

# 번역 모델. preview 계열은 예고 없이 내려갈 수 있으므로 환경변수로 바꿀 수 있게 둔다.
# 정확한 ID는 `python meta_repair.py --models` 로 확인.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite-preview")

# 실패한 조회를 영구히 캐시하지 않기 위한 재시도 간격(초).
# 네트워크 오류는 곧 복구되므로 짧게, 진짜 없는 작품은 길게 잡는다.
RETRY_AFTER_FETCH_ERROR = 6 * 3600
RETRY_AFTER_NOTFOUND = 30 * 86400

app = Flask(__name__, static_folder=str(BASE / "static"), static_url_path="/static")
app.json.ensure_ascii = False   # 한글을 \uXXXX 로 늘리지 않는다

try:   # 작품 추천 페이지(/recommend). 파일이 없으면 기능만 빠진다
    import recommend
    recommend.init(WORKS_DIR, META_DIR, CACHE_DIR)
    app.register_blueprint(recommend.bp)
except ImportError:
    recommend = None

for d in (WORKS_DIR, META_DIR, COVER_DIR):
    d.mkdir(parents=True, exist_ok=True)


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", unicodedata.normalize("NFC", s))]


LANG_SUFFIX = re.compile(r"\.(ko|kr|ja|jp|en|zh|cn|ch|chs|cht)$")
AUDIO_SUFFIX = re.compile(r"\.(mp3|wav|flac|m4a|aac|ogg|opus|mp4|webm)$")
SUBS_TAIL = re.compile(r"[-_. ]?subs?$")


def norm_stem(s):
    return unicodedata.normalize("NFC", s).strip().strip("'\" ").lower()


def sub_keys(stem):
    """자막 파일 어간에서 매칭 후보 키를 만든다.
    예: '01 인사.wav-subs' → {'01 인사.wav-subs', '01 인사.wav', '01 인사'}
        '01_本編.ko'       → {'01_本編.ko', '01_本編'}"""
    keys = {norm_stem(stem)}
    for _ in range(3):  # 꼬리(-subs·언어·오디오 확장자)가 겹쳐 있어도 차례로 벗겨냄
        for k in list(keys):
            keys.add(SUBS_TAIL.sub("", k))
            keys.add(LANG_SUFFIX.sub("", k))
            keys.add(AUDIO_SUFFIX.sub("", k))
    return {k for k in keys if k}


def looks_like_srt(p):
    """타임코드(-->)가 있는 파일만 자막으로 취급 (utf-16도 감지)."""
    try:
        head = p.read_bytes()[:4096]
    except OSError:
        return False
    return b"-->" in head or "-->".encode("utf-16-le") in head or "-->".encode("utf-16-be") in head


def decode_subtitle(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):  # BOM 을 먼저 봐야 cp932 로 잘못 풀리지 않는다
        return raw.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "utf-8", "cp932", "cp949", "utf-16"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def subtitle_lang(p):
    """자막 본문의 언어. 한국어 자막 상당수는 파일명에 .ko 가 없어서 내용으로 판별한다."""
    try:
        text = decode_subtitle(p.read_bytes())
    except OSError:
        return None
    hangul = len(HANGUL_CHARS.findall(text))
    kana = len(KANA_CHARS.findall(text))
    han = len(HAN_CHARS.findall(text))
    if hangul > kana and hangul > han:
        return "ko"
    # 일본어는 가나가 한자 못지않게 많다. 가나가 드문 한자 위주 본문은 중국어로 본다
    if kana * 5 >= han:
        return "ja" if kana else None
    return "zh"


def safe_resolve(relpath):
    """WORKS_DIR 밖으로 나가는 경로를 차단하고 절대경로를 돌려준다."""
    p = (WORKS_DIR / relpath).resolve()
    try:
        p.relative_to(WORKS_DIR.resolve())
    except ValueError:
        abort(403)
    if not p.is_file():
        abort(404)
    return p


def load_meta(rj):
    f = META_DIR / f"{rj}.json"
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return None


def save_meta(rj, meta):
    (META_DIR / f"{rj}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def meta_is_stale(meta):
    """실패로 저장된 캐시가 재시도할 때가 됐는지.

    성공 캐시는 절대 만료되지 않는다(DLsite 메타는 사실상 불변).
    checked_at이 없는 옛 캐시는 age가 무한대가 되어 한 번 재시도된 뒤 갱신된다."""
    if not meta.get("notfound"):
        return False
    limit = RETRY_AFTER_FETCH_ERROR if meta.get("error") else RETRY_AFTER_NOTFOUND
    return time.time() - (meta.get("checked_at") or 0) > limit


def needs_translation(meta):
    """제목 번역이 아직 안 된 상태인지. server.py 와 meta_repair.py 가 공유하는 단일 기준.

    title_pending 플래그는 나중에 도입돼서 그 이전 캐시에는 없다.
    그래서 '제목이 일본어인데 title_orig 가 비어 있음'도 같이 본다."""
    if not meta or meta.get("notfound"):
        return False
    if meta.get("title_pending"):
        return True
    return meta.get("title_orig") is None and bool(JA_CHARS.search(meta.get("title") or ""))


def repair_title(meta):
    """번역만 실패해 원문이 남은 캐시를 다시 번역해 고친다. 고쳐졌으면 True."""
    orig = meta.get("title_orig") or meta.get("title")
    if not orig or not JA_CHARS.search(orig):
        meta.pop("title_pending", None)  # 번역할 게 없음 — 표시만 지운다
        return True
    ko = translate_ko(orig)
    if not ko:
        return False
    meta["title"] = ko
    meta["title_orig"] = orig
    meta.pop("title_pending", None)
    return True


# ── 작품 폴더 스캔 캐시 ──────────────────────────────────────────────
# 폴더 전수 조사 비용이 작품 수에 비례해 커진다(자막 후보는 본문 앞부분까지 읽음).
# 내용이 그대로면 결과를 재사용하고, 바뀐 폴더만 다시 훑는다.
_scan_cache = {}


def dir_signature(folder):
    """폴더 내용이 바뀌었는지 판별할 값. (파일 수, 총 크기, 최신 mtime, 경로 해시)

    scandir 는 디렉터리 열거와 함께 stat 정보를 돌려주므로(윈도우) 거의 공짜다.
    파일 mtime 까지 보기 때문에 '파일 수는 그대로인데 내용만 수정' 도 잡힌다 —
    자막을 제자리에서 고쳐도 캐시가 무효화된다."""
    count = total = names = 0
    newest = 0.0
    stack = [folder]
    while stack:
        try:
            it = os.scandir(stack.pop())
        except OSError:
            continue  # 접근 불가 폴더는 건너뛴다
        with it:
            for e in it:
                # 이름만 바뀌면 수·크기·mtime 이 그대로라 경로까지 봐야 옛 경로를 내보내지 않는다
                names += zlib.crc32(e.path.encode("utf-8", "surrogatepass"))
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                        continue
                    st = e.stat()
                except OSError:
                    continue
                count += 1
                total += st.st_size
                if st.st_mtime > newest:
                    newest = st.st_mtime
    return count, total, round(newest, 3), names


def scan_work_dir(folder):
    """작품 폴더 하나를 스캔한다. 내용이 그대로면 캐시된 결과를 돌려준다.

    반환값은 호출부에서 수정하지 않는다는 전제로 공유한다."""
    sig = dir_signature(folder)
    hit = _scan_cache.get(folder.name)
    if hit is not None and hit[0] == sig:
        return hit[1]
    result = scan_work_dir_uncached(folder)
    _scan_cache[folder.name] = (sig, result)
    return result


def scan_work_dir_uncached(folder):
    """작품 폴더 하나를 스캔해 트랙·자막·로컬 표지·기타 파일을 찾는다."""
    tracks, subs, images, others = [], {}, [], []
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        rel = p.relative_to(folder)
        if ext in SUB_EXTS:
            if not looks_like_srt(p):
                # 타임코드 없는 txt는 자막이 아니라 작품의 일부(더미 파일 등)
                if ext == ".txt":
                    others.append((rel, "text"))
                continue
            # 우선순위: 한국어(.ko/.kr) → 어간 정확 일치(번역본일 가능성) → 확장자(srt>vtt>txt)
            full = norm_stem(p.stem)
            lang_pri = 0 if re.search(r"\.(ko|kr)$", full) else 1
            # 같은 폴더 우선 매칭을 위해 (부모폴더, 어간) 키와 어간 키 둘 다 저장
            for stem_key in sub_keys(p.stem):
                pri = (lang_pri, 0 if stem_key == full else 1, SUB_EXTS[ext])
                for key in ((str(rel.parent).lower(), stem_key), stem_key):
                    if key not in subs or pri < subs[key][0]:
                        subs[key] = (pri, rel)
        elif ext in IMAGE_EXTS:
            images.append(rel)
            others.append((rel, "image"))
        elif ext in AUDIO_EXTS:
            tracks.append(rel)
        elif ext in {".html", ".htm", ".pdf"}:
            others.append((rel, "file"))

    tracks.sort(key=lambda r: (natural_key(str(r.parent)), natural_key(r.name)))
    out = []
    for rel in tracks:
        stem = norm_stem(rel.stem)
        hit = subs.get((str(rel.parent).lower(), stem)) or subs.get(stem)
        sub = hit[1] if hit else None
        f = folder / rel
        out.append({
            "path": str(Path(folder.name) / rel).replace("\\", "/"),
            "name": rel.stem,
            "folder": str(rel.parent).replace("\\", "/") if str(rel.parent) != "." else "",
            "size": f.stat().st_size,
            "srt": str(Path(folder.name) / sub).replace("\\", "/") if sub else None,
            "srtLang": subtitle_lang(folder / sub) if sub else None,
        })

    cover = None
    if images:
        def img_rank(r):
            n = r.name.lower()
            pri = 0 if any(k in n for k in ("cover", "main", "表紙", "ジャケ")) else 1
            return (pri, len(str(r.parent)), natural_key(r.name))
        cover = str(Path(folder.name) / sorted(images, key=img_rank)[0]).replace("\\", "/")

    others_out = [{
        "path": str(Path(folder.name) / rel).replace("\\", "/"),
        "name": rel.name,
        "folder": str(rel.parent).replace("\\", "/") if str(rel.parent) != "." else "",
        "kind": kind,
    } for rel, kind in sorted(others, key=lambda x: (natural_key(str(x[0].parent)), natural_key(x[0].name)))]
    return out, cover, others_out


@app.route("/api/works")
def api_works():
    works = []
    entries = [p for p in WORKS_DIR.iterdir() if p.is_dir()]
    for folder in sorted(entries, key=lambda p: natural_key(p.name)):
        tracks, local_cover, others = scan_work_dir(folder)
        if not tracks:
            continue
        m = RJ_RE.search(folder.name)
        rj = m.group(0).upper() if m else None
        meta = load_meta(rj) if rj else None
        if meta and meta.get("notfound"):
            meta = None
        works.append({
            "id": folder.name,
            "rj": rj,
            "title": (meta or {}).get("title") or folder.name,
            "titleOrig": (meta or {}).get("title_orig"),
            "circle": (meta or {}).get("circle"),
            "genres": (meta or {}).get("genres") or [],
            "cvs": (meta or {}).get("cvs") or [],
            "date": (meta or {}).get("date"),
            "added": folder.stat().st_ctime,  # Windows: 폴더 생성(복사) 시각
            "hasMeta": meta is not None,
            # 제목 번역이 안 된 상태 - 프런트가 /api/meta 를 다시 불러 고치게 한다
            "titlePending": needs_translation(meta),
            "hasCover": bool(local_cover) or (meta or {}).get("image") is not None,
            "localCover": local_cover,
            "tracks": tracks,
            "others": others,
        })
    return jsonify(works)


@app.route("/api/meta/<rj>")
def api_meta(rj):
    rj = rj.upper()
    if not RJ_RE.fullmatch(rj):
        abort(400)
    meta = load_meta(rj)
    if meta is None or meta_is_stale(meta):
        meta = fetch_meta(rj)
        save_meta(rj, meta)
    elif needs_translation(meta):
        # DLsite를 다시 부를 필요 없이 번역만 재시도한다
        if repair_title(meta):
            save_meta(rj, meta)
    return jsonify(meta)


JA_CHARS = re.compile(r"[぀-ヿ一-鿿]")


def deepl_key():
    """DEEPL_API_KEY 환경변수 → deepl_key.txt 순으로 찾는다.

    매번 읽으므로 서버를 재시작하지 않고 키를 넣어도 바로 적용된다."""
    key = os.environ.get("DEEPL_API_KEY", "").strip()
    if key:
        return key
    f = BASE / "deepl_key.txt"
    if f.is_file():
        try:
            # utf-8-sig: PowerShell 의 Set-Content -Encoding utf8 이 붙이는 BOM 제거
            return f.read_text(encoding="utf-8-sig").strip()
        except OSError:
            pass
    return ""


def gemini_key():
    """GEMINI_API_KEY 환경변수 → gemini_key.txt 순으로 찾는다."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    f = BASE / "gemini_key.txt"
    if f.is_file():
        try:
            return f.read_text(encoding="utf-8-sig").strip()
        except OSError:
            pass
    return ""


def translate_ko_gemini(texts, attempts=3):
    """Gemini로 번역. 입력과 같은 길이의 리스트, 실패하면 None.

    responseSchema로 JSON 배열을 강제해 순서·개수가 어긋나지 않게 한다.
    안전필터에 막히거나(후보 없음) 개수가 안 맞으면 None → 호출부가 DeepL로 넘긴다."""
    key = gemini_key()
    if not key:
        return None
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent")

    out = []
    for i in range(0, len(texts), 50):
        chunk = texts[i:i + 50]
        body = {
            "contents": [{"parts": [{"text":
                "다음 JSON 배열의 일본어 문자열을 각각 한국어로 번역하세요.\n"
                "- 입력과 같은 순서, 같은 개수의 JSON 배열로만 답하세요.\n"
                "- ◤◢♥⛩ 같은 장식 기호와 숫자는 그대로 두세요.\n"
                "- 번역문 외에 설명을 덧붙이지 마세요.\n\n"
                + json.dumps(chunk, ensure_ascii=False)}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
        }
        got = None
        for n in range(attempts):
            try:
                r = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=30)
                if r.status_code in (400, 403):
                    print(f"  [번역] Gemini 인증/요청 오류({r.status_code}) - DeepL로 넘어갑니다.")
                    return None
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.RequestException(f"status {r.status_code}")
                r.raise_for_status()
                cands = r.json().get("candidates") or []
                if not cands or not (cands[0].get("content") or {}).get("parts"):
                    # 안전필터 차단 등 — 재시도해도 같으므로 바로 폴백
                    return None
                got = json.loads(cands[0]["content"]["parts"][0]["text"])
                if not isinstance(got, list) or len(got) != len(chunk):
                    return None
                break
            except (requests.RequestException, ValueError, LookupError, TypeError):
                if n < attempts - 1:
                    time.sleep(1.5 * (n + 1))
        if got is None:
            return None
        out.extend(got)
    return out


def translate_ko_deepl(texts, attempts=3):
    """DeepL로 번역. 입력과 같은 길이의 리스트, 실패하면 None.

    DeepL은 한 요청에 최대 50개까지 받으므로 묶어서 보낸다.
    키가 없거나 인증·할당량 문제면 재시도하지 않고 바로 포기한다."""
    key = deepl_key()
    if not key:
        return None
    # 무료 키는 ':fx' 로 끝난다 — 엔드포인트가 다르다
    url = "https://api-free.deepl.com/v2/translate" if key.endswith(":fx") \
        else "https://api.deepl.com/v2/translate"

    out = []
    for i in range(0, len(texts), 50):
        chunk = texts[i:i + 50]
        got = None
        for n in range(attempts):
            try:
                r = requests.post(
                    url,
                    headers={"Authorization": f"DeepL-Auth-Key {key}"},
                    json={"text": chunk, "source_lang": "JA", "target_lang": "KO"},
                    timeout=15,
                )
                if r.status_code == 403:
                    print("  [번역] DeepL 인증 실패(403) - 키를 확인하세요.")
                    return None
                if r.status_code == 456:
                    print("  [번역] DeepL 이번 달 무료 한도를 다 썼습니다(456).")
                    return None
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.RequestException(f"status {r.status_code}")
                r.raise_for_status()
                got = [t.get("text") for t in r.json()["translations"]]
                if len(got) != len(chunk):
                    got = None
                break
            except (requests.RequestException, ValueError, LookupError, TypeError):
                if n < attempts - 1:
                    time.sleep(1.5 * (n + 1))
        if got is None:
            return None
        out.extend(got)
    return out


def translate_ko_luna(texts):
    """OpenAI(자막과 같은 모델)로 제목을 번역한다. 용어집·교정치환·번역지시가 적용된다.
    {입력 순번: 번역} 을 돌려준다. 거절되거나 실패한 제목은 빠진다."""
    if not subgen.openai_key():
        return {}
    try:
        got = subgen.translate_titles(texts, subgen.BASE, for_files=False)
    except Exception as e:   # 키·한도·네트워크 오류 — Gemini/DeepL 로 넘긴다
        print(f"  [번역] {subgen.OPENAI_MODEL} 실패({type(e).__name__}) - 다음 번역기로 넘어갑니다.")
        return {}
    return {i: got[t] for i, t in enumerate(texts) if t in got}


def translate_ko_batch(texts):
    """OpenAI(Luna) → Gemini → DeepL 순으로, 앞에서 못 한 제목만 다음으로 넘긴다.

    입력과 같은 길이의 리스트를 돌려준다. 끝까지 번역하지 못한 자리는 None,
    하나도 번역하지 못하면 None. 호출부는 빈 자리를 title_pending 으로 표시해 뒀다가 나중에 다시 시도한다."""
    texts = list(texts)
    if not texts:
        return []
    out = [None] * len(texts)
    for i, ko in translate_ko_luna(texts).items():
        out[i] = ko
    for name, fn in (("Gemini", translate_ko_gemini), ("DeepL", translate_ko_deepl)):
        left = [i for i, ko in enumerate(out) if not ko]
        if not left:
            break
        got = fn([texts[i] for i in left])
        if got:
            for i, ko in zip(left, got):
                out[i] = ko
            print(f"  [번역] 제목 {len(left)}개를 {name}(으)로 처리했습니다.")
    if not any(out):
        if not subgen.openai_key() and not gemini_key() and not deepl_key():
            print("  [번역] 사용 가능한 키가 없습니다. openai_key.txt, gemini_key.txt, deepl_key.txt 중 하나를 만드세요.")
        return None
    return out


def translate_ko(text):
    """일본어 제목 하나를 한국어로 번역. 실패하면 None."""
    got = translate_ko_batch([text])
    return got[0] if got else None


def genre_names(item):
    """DLsite 페이지에 실제로 보이는 태그 이름.

    API 의 genres 는 원래 이름(예: 최면음성, 조교)이고, 사이트 화면은 순화된
    genres_replaced(트랜스/암시보이스, 가르침)를 보여준다. 화면과 맞추려고 후자를 쓴다."""
    genres = item.get("genres_replaced") or item.get("genres") or []
    return [g.get("name") for g in genres if g.get("name")]


def cv_names(item):
    return [c.get("name") for c in (item.get("creaters") or {}).get("voice_by") or [] if c.get("name")]


def apply_original(meta, item):
    """번역판이면 서클을 원작 서클로 바꾸고, 번역 배포처는 translator 에 남긴다.

    DLsite 번역판은 번역 배포처(예: Cream Pan)를 서클로 내세운다. 원작 번호
    (translation_info.original_workno)로 원작을 한 번 더 조회해 원작 서클을 쓴다.
    성우가 비어 있으면 원작의 성우도 가져온다. 바뀌었으면 True."""
    orig = (item.get("translation_info") or {}).get("original_workno")
    if not orig or orig == meta.get("rj"):
        return False
    try:
        r = requests.get(f"https://www.dlsite.com/maniax/api/=/product.json?workno={orig}&locale=ko_KR",
                         headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return False   # 원작 조회 실패 — 번역 배포처 이름이라도 그대로 둔다
    if not isinstance(data, list) or not data or not data[0].get("maker_name"):
        return False
    src = data[0]
    changed = False
    if meta.get("circle") != src["maker_name"]:
        meta["translator"] = item.get("maker_name")
        meta["circle"] = src["maker_name"]
        changed = True
    if not meta.get("cvs") and cv_names(src):
        meta["cvs"] = cv_names(src)
        changed = True
    return changed


def fetch_meta(rj):
    try:
        r = requests.get(
            # locale=ko_KR: 장르(태그)가 공식 한국어 명칭으로 옴
            f"https://www.dlsite.com/maniax/api/=/product.json?workno={rj}&locale=ko_KR",
            headers={"User-Agent": UA}, timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        # 네트워크·서버 오류: 작품이 없는 게 아니라 못 물어본 것 → 곧 재시도
        return {"rj": rj, "notfound": True, "error": "fetch", "checked_at": time.time()}
    if not isinstance(data, list) or not data:
        return {"rj": rj, "notfound": True, "checked_at": time.time()}
    item = data[0]
    img = (item.get("image_main") or {}).get("url")
    if img and img.startswith("//"):
        img = "https:" + img
    title = item.get("work_name")
    needs_tr = bool(title) and bool(JA_CHARS.search(title))
    title_ko = translate_ko(title) if needs_tr else None
    meta = {
        "rj": rj,
        "title": title_ko or title,
        "title_orig": title if title_ko else None,
        "circle": item.get("maker_name"),
        "image": img,
        "genres": genre_names(item),
        "cvs": cv_names(item),
        "date": item.get("regist_date"),
    }
    apply_original(meta, item)
    if needs_tr and not title_ko:
        # 메타는 멀쩡하고 번역만 실패 — 다음 조회 때 번역만 다시 시도한다
        meta["title_pending"] = True
    return meta


@app.route("/api/cover/<path:work_id>")
def api_cover(work_id):
    m = RJ_RE.search(work_id)
    rj = m.group(0).upper() if m else None

    # JSON/DLsite 표지를 먼저 사용한다. 실패하거나 표지 정보가 없을 때만 로컬 이미지를 쓴다.
    if rj:
        for ext in (".jpg", ".png", ".webp"):
            cached = COVER_DIR / (rj + ext)
            if cached.is_file():
                return send_file(cached, conditional=True)

        meta = load_meta(rj)
        url = (meta or {}).get("image")
        if url:
            try:
                r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://www.dlsite.com/"}, timeout=15)
                r.raise_for_status()
            except requests.RequestException:
                pass
            else:
                ext = os.path.splitext(url.split("?")[0])[1].lower() or ".jpg"
                cached = COVER_DIR / (rj + ext)
                cached.write_bytes(r.content)
                return send_file(cached, conditional=True)

    folder = WORKS_DIR / work_id
    if folder.is_dir():
        _, local_cover, _ = scan_work_dir(folder)
        if local_cover:
            return send_file(safe_resolve(local_cover), conditional=True)
    abort(404)


@app.route("/api/audio/<path:relpath>")
def api_audio(relpath):
    # conditional=True 로 Range 요청 지원 → 모바일에서 탐색(시킹) 가능
    return send_file(safe_resolve(relpath), conditional=True)


@app.route("/api/srt/<path:relpath>")
def api_srt(relpath):
    p = safe_resolve(relpath)
    if p.suffix.lower() not in SUB_EXTS:
        abort(400)
    text = decode_subtitle(p.read_bytes())
    return app.response_class(text, mimetype="text/plain; charset=utf-8")


@app.route("/api/subgen/failures", methods=["GET", "DELETE"])
def api_subgen_failures():
    if request.method == "DELETE":
        subgen.clear_failures()
    return jsonify(subgen.failures_snapshot())


@app.route("/api/renames")
def api_renames():
    return jsonify(subgen.rename_map())


@app.route("/api/translate-settings", methods=["GET", "POST"])
def api_translate_settings():
    if request.method == "POST":
        # JSON 요청만 받는다 — 다른 사이트의 폼 전송으로 지시문이 바뀌지 않게
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400)
        values = [data.get(k) for k in subgen.SETTINGS_FILES]
        if not all(isinstance(v, str) for v in values) or sum(map(len, values)) > 30000:
            abort(400)
        subgen.save_settings(data)
    return jsonify(subgen.load_settings())


@app.after_request
def no_stale_cache(resp):
    # HTML/JSON은 항상 서버에 재검증(304) — 폰 브라우저가 옛 UI를 캐시하는 문제 방지.
    # 오디오·표지는 그대로 캐시 가능.
    if resp.mimetype in ("text/html", "application/json", "application/manifest+json"):
        resp.headers["Cache-Control"] = "no-cache"
    return compress_response(resp)


def compress_response(resp):
    """글자 응답만 압축한다. 오디오·이미지와 Range 응답은 그대로 둔다 (시크가 깨지지 않게)."""
    if resp.status_code != 200 or resp.mimetype not in COMPRESSIBLE or "Content-Encoding" in resp.headers:
        return resp
    resp.direct_passthrough = False
    if resp.mimetype == "application/json":
        # 내용이 그대로면 304 로 답해 작품 목록을 다시 내려받지 않게 한다
        resp.add_etag(weak=True)
        resp.make_conditional(request)
        if resp.status_code == 304:
            return resp
    resp.vary.add("Accept-Encoding")
    data = resp.get_data()
    if len(data) < 500 or "gzip" not in request.headers.get("Accept-Encoding", ""):
        return resp
    resp.set_data(gzip.compress(data, 6))
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers.pop("Accept-Ranges", None)
    etag, _ = resp.get_etag()
    if etag:
        resp.set_etag(etag, weak=True)   # 압축본은 원본과 바이트가 달라 약한 ETag 로 표시한다
    return resp


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory(app.static_folder, "manifest.json")


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


if __name__ == "__main__":
    print(f"\n  작품 폴더: {WORKS_DIR}")
    o, g, d = bool(subgen.openai_key()), bool(gemini_key()), bool(deepl_key())
    if o or g or d:
        chain = " → ".join(([f"{subgen.OPENAI_MODEL}"] if o else []) + ([f"Gemini({GEMINI_MODEL})"] if g else [])
                           + (["DeepL"] if d else []))
        print(f"  제목 번역: {chain}")
    else:
        print("  [!] 번역 키 없음 - 제목 자동번역이 꺼집니다.")
        print(f"      {BASE / 'openai_key.txt'}, {BASE / 'gemini_key.txt'}, {BASE / 'deepl_key.txt'} 중 하나에 저장하면")
        print("      재시작 없이 적용됩니다.")
    print(f"  PC에서:    http://localhost:{PORT}")
    for ip in lan_ips():
        print(f"  폰에서:    http://{ip}:{PORT}  (같은 Wi-Fi)")
    print()
    if subgen.elevenlabs_key() and subgen.openai_key():
        print(f"  자막 자동 생성: ElevenLabs {subgen.ELEVENLABS_MODEL} → {subgen.OPENAI_MODEL}")
    else:
        print("  자막 자동 생성: 꺼짐 (elevenlabs_key.txt, openai_key.txt 를 만들면 켜집니다)")
    subgen.start(WORKS_DIR, scan_work_dir, dir_signature, decode_subtitle)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
