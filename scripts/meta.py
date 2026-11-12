#!/usr/bin/env python3
"""元数据与封面：查询 iTunes、下载封面、写入音频标签。

子命令：
  search  按歌名/歌手查询候选曲目（含专辑、专辑作者、年份、曲序、封面 URL）
  cover   下载封面到本地文件（默认 1000x1000）
  tag     把元数据 + 封面写入音频文件（支持 ALAC/m4a 与 FLAC）
  read    读取音频文件当前标签，用于导入后核对

用法示例：
  meta.py search --title 晴天 --artist 周杰伦
  meta.py cover --url <artworkUrl> --out /path/cover.jpg
  meta.py tag --file /path/a.m4a --meta meta.json
  meta.py read --file /path/a.m4a
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
from pathlib import Path

import requests

from _common import apply_version, emit, fail, log

UA = "iTunes/12.12 (Macintosh; OS X 10.15.7)"
# 中国区曲库常因出口 IP 被限流，多 storefront 轮询取第一个有结果且匹配度高的
STOREFRONTS = ["cn", "us", "hk", "tw", "jp", "gb"]
TIMEOUT = 20


# ---------------------------------------------------------------- iTunes

def itunes_search(term: str, limit: int = 15) -> list[dict]:
    """跨 storefront 搜索曲目，返回按相关度排序的候选。"""
    seen: dict[str, dict] = {}
    for country in STOREFRONTS:
        try:
            r = requests.get(
                "https://itunes.apple.com/search",
                params={
                    "term": term,
                    "entity": "song",
                    "limit": limit,
                    "country": country,
                    "media": "music",
                },
                headers={"User-Agent": UA},
                timeout=TIMEOUT,
            )
            results = r.json().get("results", [])
        except Exception as exc:  # 网络异常不应中断整条流程
            log(f"  [{country}] 查询失败：{exc}")
            continue
        log(f"  [{country}] 命中 {len(results)} 条")
        for item in results:
            key = f"{item.get('trackId') or item.get('collectionId')}:{item.get('trackName')}"
            if key not in seen:
                item["_storefront"] = country
                seen[key] = item
        # 中国区拿到结果就够了，避免无谓请求
        if results and country == "cn":
            break
    return list(seen.values())


def normalize(s: str) -> str:
    """去掉括号补充说明、标点、空格，便于模糊比对。"""
    s = (s or "").lower()
    s = re.sub(r"[\(（\[【].*?[\)）\]】]", " ", s)   # (Live) 【正式版】等
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def score(item: dict, title: str, artist: str) -> float:
    """相关度：歌名权重最高，歌手次之，专辑名兜底。"""
    t = difflib.SequenceMatcher(None, normalize(title), normalize(item.get("trackName", ""))).ratio()
    a = difflib.SequenceMatcher(None, normalize(artist), normalize(item.get("artistName", ""))).ratio()
    s = t * 0.7 + a * 0.3
    if normalize(title) and normalize(title) in normalize(item.get("trackName", "")):
        s += 0.1
    return round(min(s, 1.0), 3)


def artwork_url(item: dict, size: int = 1000) -> str:
    """把 100x100bb.jpg 换成更高分辨率。"""
    url = item.get("artworkUrl100") or ""
    if not url:
        return ""
    return re.sub(r"/\d+x\d+bb\.(jpg|png)$", f"/{size}x{size}bb.jpg", url)


def to_meta(item: dict, size: int = 1000) -> dict:
    """把 iTunes 结果收敛成标签字段。"""
    date = item.get("releaseDate") or ""
    return {
        "title": item.get("trackName", ""),
        "artist": item.get("artistName", ""),
        "album": item.get("collectionName", ""),
        # 专辑作者优先取 albumArtist，缺省退回 artist —— 这是"专辑作者"的正确来源
        "album_artist": item.get("collectionArtistName") or item.get("artistName", ""),
        "composer": item.get("composerName", ""),
        "genre": item.get("primaryGenreName", ""),
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "track_number": item.get("trackNumber"),
        "track_count": item.get("trackCount"),
        "disc_number": item.get("discNumber"),
        "disc_count": item.get("discCount"),
        "cover_url": artwork_url(item, size),
        "storefront": item.get("_storefront", ""),
        "itunes_id": item.get("trackId") or item.get("collectionId"),
    }


# ---------------------------------------------------------------- 封面

def download_cover(url: str, out: Path) -> Path:
    """下载封面；高分辨率失败时降级到 600/100。"""
    candidates = [url]
    for size in (600, 100):
        alt = re.sub(r"/\d+x\d+bb\.jpg$", f"/{size}x{size}bb.jpg", url)
        if alt != url and alt not in candidates:
            candidates.append(alt)
    last_err = ""
    for u in candidates:
        try:
            r = requests.get(u, headers={"User-Agent": UA}, timeout=TIMEOUT)
            if r.status_code == 200 and len(r.content) > 2000:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(r.content)
                log(f"  封面已下载 {len(r.content) // 1024} KB → {out.name}")
                return out
            last_err = f"HTTP {r.status_code}, {len(r.content)} bytes"
        except Exception as exc:
            last_err = str(exc)
    raise OSError(f"封面下载失败：{last_err}")


# ---------------------------------------------------------------- 写标签

def tag_m4a(path: Path, meta: dict, cover: Path | None) -> None:
    """ALAC/AAC 的 MP4 原子标签。"""
    from mutagen.mp4 import MP4, MP4Cover

    audio = MP4(str(path))
    if audio.tags is None:
        audio.add_tags()
    t = audio.tags
    mapping = {
        "©nam": meta.get("title"),
        "©ART": meta.get("artist"),
        "©alb": meta.get("album"),
        "aART": meta.get("album_artist"),
        "©wrt": meta.get("composer"),
        "©gen": meta.get("genre"),
        "©day": str(meta["year"]) if meta.get("year") else None,
        "©too": "music-downloader",
    }
    for key, value in mapping.items():
        if value:
            t[key] = [value]
        elif key in t:
            del t[key]
    if meta.get("track_number"):
        t["trkn"] = [(int(meta["track_number"]), int(meta.get("track_count") or 0))]
    if meta.get("disc_number"):
        t["disk"] = [(int(meta["disc_number"]), int(meta.get("disc_count") or 0))]
    if cover and cover.exists():
        data = cover.read_bytes()
        fmt = MP4Cover.FORMAT_PNG if data[:8] == b"\x89PNG\r\n\x1a\n" else MP4Cover.FORMAT_JPEG
        t["covr"] = [MP4Cover(data, imageformat=fmt)]
    audio.save()


def tag_flac(path: Path, meta: dict, cover: Path | None) -> None:
    """FLAC 的 Vorbis comment + 独立 PICTURE 块。"""
    from mutagen.flac import FLAC, Picture

    audio = FLAC(str(path))
    mapping = {
        "title": meta.get("title"),
        "artist": meta.get("artist"),
        "album": meta.get("album"),
        "albumartist": meta.get("album_artist"),
        "composer": meta.get("composer"),
        "genre": meta.get("genre"),
        "date": str(meta["year"]) if meta.get("year") else None,
    }
    for key, value in mapping.items():
        if value:
            audio[key] = [value]
        elif key in audio:
            del audio[key]
    if meta.get("track_number"):
        audio["tracknumber"] = [f"{meta['track_number']}/{meta.get('track_count') or ''}".rstrip("/")]
    if meta.get("disc_number"):
        audio["discnumber"] = [f"{meta['disc_number']}/{meta.get('disc_count') or ''}".rstrip("/")]
    if cover and cover.exists():
        pic = Picture()
        pic.type = 3  # front cover
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.data = cover.read_bytes()
        audio.clear_pictures()
        audio.add_picture(pic)
    audio.save()


def tag_mp3(path: Path, meta: dict, cover: Path | None) -> None:
    """MP3 的 ID3v2.3 标签 + APIC 封面。

    用 2.3 而不是 2.4：Music/iTunes 对 2.3 的读取最稳，
    2.4 的部分帧（尤其 TDRC 年份）在旧版本里会被忽略。
    """
    from mutagen.id3 import (APIC, ID3, TALB, TCOM, TCON, TDRC, TIT2, TPE1,
                             TPE2, TPOS, TRCK, ID3NoHeaderError)

    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()

    simple = {
        "TIT2": (TIT2, meta.get("title")),
        "TPE1": (TPE1, meta.get("artist")),
        "TALB": (TALB, meta.get("album")),
        "TPE2": (TPE2, meta.get("album_artist") or meta.get("artist")),
        "TCOM": (TCOM, meta.get("composer")),
        "TCON": (TCON, meta.get("genre")),
        "TDRC": (TDRC, str(meta["year"]) if meta.get("year") else None),
    }
    for key, (frame_cls, value) in simple.items():
        if value:
            tags.setall(key, [frame_cls(encoding=3, text=[str(value)])])
        elif key in tags:
            tags.delall(key)

    if meta.get("track_number"):
        num, total = int(meta["track_number"]), int(meta.get("track_count") or 0)
        tags.setall("TRCK", [TRCK(encoding=3, text=[f"{num}/{total}" if total else str(num)])])
    if meta.get("disc_number"):
        num, total = int(meta["disc_number"]), int(meta.get("disc_count") or 0)
        tags.setall("TPOS", [TPOS(encoding=3, text=[f"{num}/{total}" if total else str(num)])])

    if cover and cover.exists():
        data = cover.read_bytes()
        mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))

    tags.save(str(path), v2_version=3)


def read_tags(path: Path) -> dict:
    """读取标签，供导入后核对。"""
    from mutagen import File as MutaFile

    f = MutaFile(str(path))
    if f is None:
        return {}
    cls = f.__class__.__name__
    if cls == "MP3":
        t = f.tags or {}
        one = lambda k: (str(t[k].text[0]) if t.get(k) and getattr(t[k], "text", None) else None)  # noqa: E731
        return {
            "title": one("TIT2"), "artist": one("TPE1"), "album": one("TALB"),
            "album_artist": one("TPE2"), "composer": one("TCOM"), "genre": one("TCON"),
            "year": one("TDRC"), "track": one("TRCK"),
            "cover": bool(t.get("APIC:") or any(k.startswith("APIC") for k in t)),
            "duration": round(f.info.length, 1),
            "bitrate": getattr(f.info, "bitrate", 0),
            "codec": "mp3",
        }
    if cls == "MP4":
        g = lambda k: (f.tags.get(k) or [None])[0]  # noqa: E731
        trkn = (f.tags.get("trkn") or [(None, None)])[0]
        return {
            "title": g("©nam"), "artist": g("©ART"), "album": g("©alb"),
            "album_artist": g("aART"), "composer": g("©wrt"), "genre": g("©gen"),
            "year": g("©day"),
            "track": trkn[0] if trkn else None,
            "cover": bool(f.tags.get("covr")),
            "duration": round(f.info.length, 1),
            "bitrate": getattr(f.info, "bitrate", 0),
            "codec": getattr(f.info, "codec", ""),
        }
    d = f.tags or {}
    return {
        "title": (d.get("title") or [None])[0],
        "artist": (d.get("artist") or [None])[0],
        "album": (d.get("album") or [None])[0],
        "album_artist": (d.get("albumartist") or [None])[0],
        "composer": (d.get("composer") or [None])[0],
        "genre": (d.get("genre") or [None])[0],
        "year": (d.get("date") or [None])[0],
        "cover": bool(getattr(f, "pictures", [])),
        "duration": round(f.info.length, 1),
    }


# ---------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser(description="元数据与封面工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_search = sub.add_parser("search", help="查询曲目元数据")
    p_search.add_argument("--title", required=True)
    p_search.add_argument("--artist", default="")
    p_search.add_argument("--album", default="")
    p_search.add_argument("--limit", type=int, default=12)

    p_cover = sub.add_parser("cover", help="下载封面")
    p_cover.add_argument("--url", required=True)
    p_cover.add_argument("--out", required=True)

    p_tag = sub.add_parser("tag", help="写入标签")
    p_tag.add_argument("--file", required=True)
    p_tag.add_argument("--meta", required=True, help="元数据 JSON 文件路径")
    p_tag.add_argument("--cover", default="", help="本地封面图路径")
    p_tag.add_argument("--version", default="",
                       help="版本后缀：翻唱版 / 剧情版 / 纯歌版 / 伴奏版 / 现场版… "
                            "会拼成「歌名（版本）」，meta.json 里的 version 字段等价")

    p_read = sub.add_parser("read", help="读取标签")
    p_read.add_argument("--file", required=True)

    args = ap.parse_args()

    if args.cmd == "search":
        term = " ".join(x for x in [args.artist, args.title] if x).strip()
        log(f"查询 iTunes：{term}")
        items = itunes_search(term, args.limit)
        if not items:
            fail("iTunes 未找到匹配曲目", term=term)
        ranked = sorted(
            ({"score": score(i, args.title, args.artist), **to_meta(i)} for i in items),
            key=lambda x: x["score"],
            reverse=True,
        )
        # 同专辑多条时升序排列曲序，方便 Agent 判断整张专辑
        top = [r for r in ranked if r["score"] >= 0.45][:8]
        emit({"ok": True, "term": term, "candidates": top or ranked[:5]})

    elif args.cmd == "cover":
        try:
            p = download_cover(args.url, Path(args.out))
            emit({"ok": True, "path": str(p), "bytes": p.stat().st_size})
        except Exception as exc:
            fail(str(exc))

    elif args.cmd == "tag":
        path = Path(args.file)
        if not path.exists():
            fail(f"文件不存在：{path}")
        meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
        if args.version:
            meta["version"] = args.version
        meta = apply_version(meta)      # 翻唱版/剧情版等后缀并进 title
        cover = Path(args.cover) if args.cover else None
        try:
            suffix = path.suffix.lower()
            if suffix == ".flac":
                tag_flac(path, meta, cover)
            elif suffix == ".mp3":
                tag_mp3(path, meta, cover)
            else:
                tag_m4a(path, meta, cover)
            emit({"ok": True, "file": str(path), "tags": read_tags(path)})
        except Exception as exc:
            fail(f"写标签失败：{exc}")

    elif args.cmd == "read":
        path = Path(args.file)
        if not path.exists():
            fail(f"文件不存在：{path}")
        emit({"ok": True, "file": str(path), "tags": read_tags(path)})


if __name__ == "__main__":
    main()
