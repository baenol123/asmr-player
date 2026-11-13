# -*- coding: utf-8 -*-
"""cache\meta 에 굳어버린 실패 캐시를 찾아 고친다.

server.py 가 실패를 영구 캐시하던 시절에 만들어진 파일들이 대상:
  - 번역 실패: 제목이 일본어인데 title_orig 가 None  → 번역만 다시 시도
  - 조회 실패: notfound 인데 checked_at 이 없음      → 지워서 다음 접속 때 재조회

기본은 미리보기(dry-run). 실제로 반영하려면 --apply 를 붙인다.
  python meta_repair.py            # 무엇을 고칠지 보여주기만
  python meta_repair.py --apply    # 실제 수정
  python meta_repair.py --apply --only RJ01234567

태그만 DLsite 에서 새로 받기 (제목·번역은 그대로 둔다):
  python meta_repair.py --tags            # 바뀔 태그 미리보기
  python meta_repair.py --tags --apply    # 실제 수정
"""
import argparse
import sys

# 콘솔 기본 인코딩(한국어 윈도우는 cp949)에 없는 글자로 죽지 않게 UTF-8로 고정
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from server import (META_DIR, deepl_key, gemini_key, load_meta, needs_translation, save_meta,
                    translate_ko_batch)

p = argparse.ArgumentParser()
p.add_argument("--apply", action="store_true", help="실제로 파일을 수정한다")
p.add_argument("--only", metavar="RJ", help="이 RJ코드 하나만 처리")
p.add_argument("--models", action="store_true", help="Gemini에서 쓸 수 있는 모델 ID를 보여준다")
p.add_argument("--tags", action="store_true", help="태그만 DLsite 에서 새로 받는다 (제목은 그대로)")
args = p.parse_args()

if args.tags:
    import time
    import requests
    from server import UA, genre_names
    files = sorted(META_DIR.glob("*.json"))
    if args.only:
        files = [f for f in files if f.stem.upper() == args.only.upper()]
    changed = same = failed = 0
    for f in files:
        meta = load_meta(f.stem)
        if not meta or meta.get("notfound"):
            continue
        try:
            for wait in (30, 60, 120, None):
                r = requests.get(f"https://www.dlsite.com/maniax/api/=/product.json?workno={f.stem}&locale=ko_KR",
                                 headers={"User-Agent": UA}, timeout=15)
                if r.status_code != 429 or wait is None:
                    break
                # 요청이 너무 많다는 응답 — 잠시 쉬었다가 다시 묻는다
                print(f"  … DLsite 가 잠시 쉬라고 해서 {wait}초 기다립니다")
                time.sleep(wait)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  X {f.stem}  조회 실패: {e}")
            failed += 1
            continue
        if not isinstance(data, list) or not data:
            print(f"  X {f.stem}  DLsite 에 정보 없음 (판매 종료 등) — 그대로 둠")
            failed += 1
            continue
        new = genre_names(data[0])
        old = meta.get("genres") or []
        if new == old:
            same += 1
        else:
            gone = [t for t in old if t not in new]
            added = [t for t in new if t not in old]
            print(f"  {f.stem}  -{', '.join(gone) or '없음'}  +{', '.join(added) or '없음'}")
            changed += 1
            if args.apply:
                meta["genres"] = new
                save_meta(f.stem, meta)
        time.sleep(1)   # DLsite 에 연달아 보내지 않게 (너무 빠르면 429 로 막힌다)
    print(f"\n바뀜 {changed} / 같음 {same} / 실패 {failed}")
    if changed and not args.apply:
        print("--tags --apply 로 실행하면 실제로 수정합니다.")
    sys.exit(0)

if args.models:
    import requests
    from server import GEMINI_MODEL
    key = gemini_key()
    if not key:
        sys.exit("GEMINI_API_KEY 가 없습니다. gemini_key.txt 를 만드세요.")
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                     headers={"x-goog-api-key": key}, timeout=15)
    r.raise_for_status()
    print(f"현재 설정: {GEMINI_MODEL}\n")
    for m in r.json().get("models", []):
        if "generateContent" in (m.get("supportedGenerationMethods") or []):
            name = m["name"].removeprefix("models/")
            print(f"  {'*' if name == GEMINI_MODEL else ' '} {name}")
    sys.exit(0)

files = sorted(META_DIR.glob("*.json"))
if args.only:
    files = [f for f in files if f.stem.upper() == args.only.upper()]
    if not files:
        sys.exit(f"캐시 없음: {args.only}")

tr_targets, nf_targets, ok = [], [], 0
for f in files:
    meta = load_meta(f.stem)
    if meta is None:
        print(f"  ! 읽기 실패(손상): {f.name}")
        continue
    if meta.get("notfound"):
        # checked_at 이 있으면 새 형식이라 server.py 의 쿨다운이 알아서 재시도한다
        if meta.get("checked_at") is None:
            nf_targets.append(f.stem)
        continue
    if needs_translation(meta):
        tr_targets.append((f.stem, meta, meta.get("title") or ""))
    else:
        ok += 1

print(f"정상 {ok} / 번역실패 {len(tr_targets)} / 재조회필요 {len(nf_targets)}\n")

if tr_targets:
    print("[번역 재시도]")
    if not args.apply:
        for rj, _meta, title in tr_targets:
            print(f"  - {rj}  {title[:60]}")
    elif not gemini_key() and not deepl_key():
        print("  번역 키가 없어 건너뜁니다. gemini_key.txt 또는 deepl_key.txt 를 만드세요.")
    else:
        # 하나씩 부르지 않고 묶어서 보낸다
        result = translate_ko_batch([t for _rj, _m, t in tr_targets])
        for (rj, meta, title), ko in zip(tr_targets, result or [None] * len(tr_targets)):
            if ko:
                meta["title"], meta["title_orig"] = ko, title
                meta.pop("title_pending", None)
                print(f"  O {rj}  {ko[:60]}")
            else:
                # 여전히 실패 - 표시를 남겨 서버가 다음에 또 시도하게 한다
                meta["title_pending"] = True
                print(f"  X {rj}  번역 실패, title_pending 표시함")
            save_meta(rj, meta)

if nf_targets:
    print("\n[재조회 대상 - 캐시 삭제]")
    for rj in nf_targets:
        if args.apply:
            (META_DIR / f"{rj}.json").unlink()
        print(f"  {'삭제' if args.apply else '삭제 예정'}: {rj}")

if not args.apply and (tr_targets or nf_targets):
    print("\n--apply 를 붙여 실행하면 실제로 수정합니다.")
