#!/usr/bin/env python3
"""B 站兜底搜索：hifini 找不到时，在 B 站找可下载的音频来源。

两条路：
1. **API 优先**（`api.bilibili.com/x/web-interface/search/type`）——匿名请求即可用，
   不受 search.bilibili.com 的验证码风控影响，是目前最稳的一条。
2. **页面兜底**（search.bilibili.com 服务端渲染）——风控一开就返回「验证码_哔哩哔哩」，
   解析不到任何卡片，只作为 API 全挂时的退路。

用法：
  bilibili.py search --query "晴天 周杰伦" [--limit 12] [--pages 2]
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import requests
from bs4 import BeautifulSoup

from _common import emit, fail, log

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 明显不是「歌曲本体」的标题，降权处理
NOISE = ["教学", "鼓谱", "吉他谱", "简谱", "谱子", "翻唱", "钢琴版", "伴奏", "指弹",
         "教程", "教程", "reaction", "解说", "合集", "混剪", "MV解析"]
# 更像纯音频/无损的标题，加权
BOOST = ["无损", "原曲", "纯音频", "音频", "完整版", "高音质", "HQ", "flac", "动态歌词"]

API = "https://api.bilibili.com/x/web-interface/search/type"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
})


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").replace("&quot;", '"').replace("&amp;", "&")


def search_api(keyword: str, page: int = 1, retries: int = 3) -> list[dict]:
    """匿名搜索 API。风控时会返回 HTML 或 code != 0，重试几次仍不行就抛异常。"""
    params = {"search_type": "video", "keyword": keyword, "page": page}
    last = "未知错误"
    for attempt in range(retries):
        try:
            r = SESSION.get(API, params=params, timeout=25)
            d = r.json()
        except Exception as exc:
            last = f"HTTP {r.status_code} / {type(exc).__name__}" if "r" in locals() else str(exc)
            time.sleep(1.0 + attempt)
            continue
        if d.get("code") != 0:
            last = f"code={d.get('code')} {d.get('message')}"
            time.sleep(1.0 + attempt)
            continue
        items = []
        for it in (d.get("data") or {}).get("result") or []:
            bvid = it.get("bvid") or ""
            if not bvid:
                continue
            items.append({
                "bvid": bvid,
                "title": _strip_tags(it.get("title", "")),
                "url": f"https://www.bilibili.com/video/{bvid}",
                "author": it.get("author", ""),
                "duration": it.get("duration", ""),
                "pubdate": it.get("pubdate", 0),
                "play": it.get("play", 0),
                "desc": _strip_tags(it.get("description", ""))[:120],
            })
        return items
    raise OSError(last)


def fetch(keyword: str) -> str:
    url = f"https://search.bilibili.com/all?keyword={quote(keyword)}"
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/",
               "Accept-Language": "zh-CN,zh;q=0.9"}
    r = requests.get(url, headers=headers, timeout=25)
    if r.status_code != 200:
        raise OSError(f"B 站返回 HTTP {r.status_code}")
    return r.text


def parse(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    items = []
    seen = set()
    for card in soup.select(".bili-video-card"):
        a = card.select_one('a[href*="/video/BV"]')
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r"(BV[0-9A-Za-z]{10})", href)
        if not m:
            continue
        bvid = m.group(1)
        if bvid in seen:
            continue
        seen.add(bvid)

        title = ""
        tit_el = card.select_one(".bili-video-card__info--tit")
        if tit_el:
            title = tit_el.get_text(" ", strip=True)
        if not title:
            img = card.select_one("img[alt]")
            title = img.get("alt", "").strip() if img else ""
        if not title:
            continue

        author = ""
        au = card.select_one(".bili-video-card__info--author")
        if au:
            author = au.get_text(" ", strip=True)
        dur = ""
        d = card.select_one(".bili-video-card__stats__duration")
        if d:
            dur = d.get_text(strip=True)

        items.append({
            "bvid": bvid,
            "title": title,
            "url": f"https://www.bilibili.com/video/{bvid}",
            "author": author,
            "duration": dur,
            "pubdate": 0,
            "play": 0,
            "desc": "",
        })
    return items


def rank(items: list[dict], title: str, artist: str) -> list[dict]:
    """按与目标歌曲的贴合度排序，并给出提示标签。"""
    core = [w for w in re.split(r"\s+", f"{title} {artist}".strip()) if w]
    scored = []
    for it in items:
        t = it["title"]
        s = 0.0
        for w in core:
            if w and w in t:
                s += 1.0
        if any(w in t for w in BOOST):
            s += 0.8
        if any(w in t for w in NOISE):
            s -= 1.2
        # 标题越短通常越接近单曲本体
        s -= min(len(t), 60) / 200.0
        it = {**it, "score": round(s, 2)}
        scored.append(it)
    return sorted(scored, key=lambda x: x["score"], reverse=True)


def _cmd_search(args: argparse.Namespace) -> None:
    log(f"在 B 站搜索：{args.query}")

    items: list[dict] = []
    source = "api"
    try:
        for page in range(1, max(1, args.pages) + 1):
            got = search_api(args.query, page=page)
            items.extend(got)
            if len(got) < 20:
                break
            if page < args.pages:
                time.sleep(0.6)
        log(f"  API 抓到 {len(items)} 个视频")
    except Exception as exc:
        log(f"  API 不可用（{exc}），回退页面抓取")
        source = "page"
        try:
            html = fetch(args.query)
        except Exception as exc2:
            fail(f"B 站搜索失败：{exc2}")
        items = parse(html)
        log(f"  页面解析到 {len(items)} 个视频")
        if not items:
            fail("B 站搜索被风控拦截（返回验证码页）且 API 也不可用；"
                 "请稍后重试，或直接把 BV 号/链接传给 downie.py download")

    # 按 bvid 去重，保留先出现的
    uniq, seen = [], set()
    for it in items:
        if it["bvid"] in seen:
            continue
        seen.add(it["bvid"])
        uniq.append(it)
    items = uniq

    # 用查询词里的歌名/歌手做加权
    parts = re.split(r"\s+", args.query.strip())
    title = parts[0] if parts else args.query
    artist = parts[1] if len(parts) > 1 else ""

    ranked = rank(items, title, artist)[: args.limit]
    emit({"ok": True, "query": args.query, "source": source, "count": len(ranked),
          "results": [
              {**r, "note": "优先候选" if r["score"] >= 1.0 else ""}
              for r in ranked
          ]})


# ---------------- 分 P 音频直取 ----------------
# 多 P 视频（官方 MV 常把「剧情版 / 纯享版 / 带音效 / 带念白」拆成 P1..Pn）
# 交给 Downie 不可靠：`?p=4` 常常只解析到 P1 或直接卡在解析阶段。
# 这里直接走 playurl 拿 DASH 音轨，匿名最高 30280（≈184kbps AAC 48kHz）。

WBI_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _wbi_key() -> str:
    d = SESSION.get("https://api.bilibili.com/x/web-interface/nav", timeout=25).json()
    wbi = (d.get("data") or {}).get("wbi_img") or {}
    img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    raw = img + sub
    if len(raw) < 64:
        raise OSError("没拿到 wbi key（nav 接口异常）")
    return "".join(raw[i] for i in WBI_TAB)[:32]


def _sign(params: dict, key: str) -> str:
    p = dict(params)
    p["wts"] = int(time.time())
    q = urlencode([(k, re.sub(r"[!'()*]", "", str(v))) for k, v in sorted(p.items())])
    return q + "&w_rid=" + hashlib.md5((q + key).encode()).hexdigest()


def video_pages(bvid: str) -> tuple[str, list[dict]]:
    d = SESSION.get("https://api.bilibili.com/x/web-interface/view",
                    params={"bvid": bvid}, timeout=25).json()
    if d.get("code") != 0:
        raise OSError(f"取视频信息失败：{d.get('code')} {d.get('message')}")
    data = d["data"]
    return data.get("title", ""), data.get("pages") or []


def page_audio(bvid: str, cid: int) -> dict:
    key = _wbi_key()
    q = _sign({"bvid": bvid, "cid": cid, "fnval": 16, "fourk": 1, "fnver": 0}, key)
    d = SESSION.get(f"https://api.bilibili.com/x/player/wbi/playurl?{q}", timeout=25).json()
    if d.get("code") != 0:
        raise OSError(f"playurl 失败：{d.get('code')} {d.get('message')}")
    audios = ((d.get("data") or {}).get("dash") or {}).get("audio") or []
    if not audios:
        raise OSError("该 P 没有 DASH 音频流（可能已失效或需要登录）")
    return max(audios, key=lambda a: a.get("bandwidth") or 0)


def cmd_audio(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    title, pages = video_pages(args.bvid)
    if not pages:
        fail(f"{args.bvid} 没有分 P 信息")
    log(f"《{title}》共 {len(pages)} P")

    if args.p in ("", "all"):
        want = {p["page"] for p in pages}
    else:
        want = {int(x) for x in args.p.split(",") if x.strip().isdigit()}

    results = []
    for p in pages:
        if p["page"] not in want:
            continue
        audio = page_audio(args.bvid, p["cid"])
        out = out_dir / f"{args.bvid}_P{p['page']}.m4a"
        with SESSION.get(audio["baseUrl"], headers={
            "Referer": f"https://www.bilibili.com/video/{args.bvid}",
            "Origin": "https://www.bilibili.com",
        }, stream=True, timeout=90) as r:
            r.raise_for_status()
            with open(out, "wb") as fh:
                for chunk in r.iter_content(262144):
                    fh.write(chunk)
        log(f"  P{p['page']} {p.get('part','')} → {out.name} ({out.stat().st_size // 1024} KB)")
        results.append({
            "page": p["page"],
            "part": p.get("part", ""),
            "cid": p["cid"],
            "duration": p.get("duration"),
            "bandwidth": audio.get("bandwidth"),
            "codecs": audio.get("codecs"),
            "file": str(out),
            "size": out.stat().st_size,
        })

    if not results:
        fail(f"没有匹配的 P（你要的是 {sorted(want)}，视频只有 1..{len(pages)}）")
    emit({"ok": True, "bvid": args.bvid, "title": title,
          "count": len(results), "files": results,
          "next": "用 convert.py run --file <m4a> --format mp3 --bitrate 320 转成 320k MP3"})


def main() -> None:
    ap = argparse.ArgumentParser(description="B 站搜索 / 分 P 音频下载")
    ap.add_argument("cmd", nargs="?", default="search", choices=["search", "audio"],
                    help="search=按关键词搜；audio=按 BV 号取某个 P 的音轨")
    ap.add_argument("--query", default="", help="search：关键词")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--pages", type=int, default=1,
                    help="search：API 抓几页（每页 20 条），默认 1")
    ap.add_argument("--bvid", default="", help="audio：BV 号")
    ap.add_argument("--p", default="all", help="audio：P 号，逗号分隔或 all")
    ap.add_argument("--out-dir", default="", help="audio：下载目录")

    args = ap.parse_args()

    if args.cmd == "audio":
        if not args.bvid or not args.out_dir:
            fail("audio 需要 --bvid 和 --out-dir")
        try:
            cmd_audio(args)
        except OSError as exc:
            fail(f"取音频失败：{exc}")
        return

    if not args.query:
        fail("search 需要 --query")
    _cmd_search(args)


if __name__ == "__main__":
    main()
