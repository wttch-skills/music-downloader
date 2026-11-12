#!/usr/bin/env python3
"""把音频文件导入 Apple Music，并写正确元数据与封面。

做法是「文件先打好标签 → 导入 → 再用脚本核对并补写」：
  · 导入前标签由 meta.py tag 写好，Music 导入时会读取，多数情况已经正确；
  · 导入后仍逐字段设置一遍，避免 Music 缓存旧值或标签解析差异；
  · 封面额外通过 artwork 属性写入，因为部分来源的图片 Music 不会自动认。

用法：
  music_lib.py import --file a.m4a --meta meta.json [--cover cover.jpg] [--replace]
  music_lib.py find --title 晴天 --artist 周杰伦
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from _common import apply_version, emit, fail, log, normalize_version, osascript


def esc(s: str) -> str:
    """AppleScript 字符串转义。"""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


IMPORT_TMPL = '''
set fpath to "%(path)s"
tell application "Music"
  set targetFile to (POSIX file fpath) as alias
  set added to add targetFile
  if class of added is list then
    set t to item 1 of added
  else
    set t to added
  end if
  delay 0.4
  %(fields)s
  return (database ID of t) as text
end tell
'''

SET_FIELDS_TMPL = '''
  try
    set name of t to "%(title)s"
  end try
  try
    set artist of t to "%(artist)s"
  end try
  try
    set album of t to "%(album)s"
  end try
  try
    set album artist of t to "%(album_artist)s"
  end try
  try
    set composer of t to "%(composer)s"
  end try
  try
    set genre of t to "%(genre)s"
  end try
  %(year)s
  %(track)s
  %(disc)s
'''

COVER_TMPL = '''
tell application "Music"
  set t to (first track of library playlist 1 whose database ID is %(dbid)s)
  set img to (read (POSIX file "%(cover)s") as JPEG picture)
  try
    if (count of artworks of t) > 0 then
      delete artwork 1 of t
    end if
  end try
  set data of (make new artwork at t) to img
end tell
'''

READBACK_TMPL = '''
tell application "Music"
  set t to (first track of library playlist 1 whose database ID is %(dbid)s)
  set out to ""
  set out to out & "name=" & (name of t) & linefeed
  set out to out & "artist=" & (artist of t) & linefeed
  set out to out & "album=" & (album of t) & linefeed
  set out to out & "albumartist=" & (album artist of t) & linefeed
  set out to out & "composer=" & (composer of t) & linefeed
  set out to out & "genre=" & (genre of t) & linefeed
  set out to out & "year=" & (year of t as text) & linefeed
  set out to out & "track=" & (track number of t as text) & linefeed
  set out to out & "artworks=" & (count of artworks of t) as text
  return out
end tell
'''

FIND_TMPL = '''
tell application "Music"
  set ms to every track of library playlist 1 whose name contains "%(title)s"
  set out to ""
  repeat with t in ms
    set out to out & (database ID of t as text) & "|" & (name of t) & "|" & (artist of t) & "|" & (album of t) & "|" & (location of t as text) & linefeed
  end repeat
  return out
end tell
'''


def build_fields(meta: dict) -> str:
    """拼出设置字段的 AppleScript 片段。"""
    year = ""
    if meta.get("year"):
        year = f'try\n    set year of t to {int(meta["year"])}\n  end try'
    track = ""
    if meta.get("track_number"):
        track = f'try\n    set track number of t to {int(meta["track_number"])}\n  end try'
        if meta.get("track_count"):
            track += f'\n  try\n    set track count of t to {int(meta["track_count"])}\n  end try'
    disc = ""
    if meta.get("disc_number"):
        disc = f'try\n    set disc number of t to {int(meta["disc_number"])}\n  end try'

    return SET_FIELDS_TMPL % {
        "title": esc(meta.get("title", "")),
        "artist": esc(meta.get("artist", "")),
        "album": esc(meta.get("album", "")),
        "album_artist": esc(meta.get("album_artist") or meta.get("artist", "")),
        "composer": esc(meta.get("composer", "")),
        "genre": esc(meta.get("genre", "")),
        "year": year,
        "track": track,
        "disc": disc,
    }


def readback(dbid: str) -> dict:
    out = osascript(READBACK_TMPL % {"dbid": dbid}, timeout=60)
    data = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


ARTWORK_TMPL = '''
tell application "Music"
  set out to ""
  set ms to (every track of library playlist 1 whose name contains "%(title)s")
  repeat with t in ms
    set n to 0
    try
      set n to count of artworks of t
    end try
    set out to out & (name of t) & "|" & (n as text) & linefeed
  end repeat
  return out
end tell
'''


def artwork_counts(title: str) -> dict[str, int]:
    """查库里这些曲目的封面数。注意：Music 长时间不重启时这个属性会一路返回 0，
    那不代表没封面 —— 真伪要用 restart_music() 后复测。"""
    try:
        out = osascript(ARTWORK_TMPL % {"title": esc(title)}, timeout=60)
    except OSError:
        return {}
    res: dict[str, int] = {}
    for line in out.splitlines():
        if "|" not in line:
            continue
        name, _, num = line.rpartition("|")
        try:
            res[name.strip()] = int(num.strip())
        except ValueError:
            continue
    return res


def _music_running() -> bool:
    return subprocess.run(["/usr/bin/pgrep", "-x", "Music"],
                          capture_output=True).returncode == 0


def restart_music(wait: int = 12) -> str:
    """退出再打开「音乐」，逼它重新解析文件标签里的封面。

    为什么必须重启：新导入的曲目 Music 不会立刻索引文件里的 APIC；而用 AppleScript
    写 artwork 在这个版本上总是失败（「不能生成类 artwork」）。实测重启一次后
    两首的 artworks 都变成 1 —— 之前判断「Music 的 artwork 坏了」是错的，
    实际只是没重读。
    """
    try:
        osascript('tell application "Music" to quit', timeout=90)
    except OSError:
        pass
    for _ in range(60):                 # 等它真的退出，最多 30s
        if not _music_running():
            break
        time.sleep(0.5)
    else:
        subprocess.run(["/usr/bin/killall", "Music"], capture_output=True)
        time.sleep(3)

    subprocess.run(["/usr/bin/open", "-a", "Music"], capture_output=True)
    for _ in range(60):                 # 等启动
        if _music_running():
            break
        time.sleep(0.5)
    time.sleep(wait)                    # 留时间给它建索引
    return "已重启音乐"


def main() -> None:
    ap = argparse.ArgumentParser(description="导入 Apple Music")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_imp = sub.add_parser("import", help="导入文件并设置元数据")
    p_imp.add_argument("--file", required=True)
    p_imp.add_argument("--meta", required=True, help="元数据 JSON")
    p_imp.add_argument("--cover", default="", help="本地封面图")
    p_imp.add_argument("--version", default="",
                       help="版本后缀：翻唱版 / 剧情版 / 纯歌版 / 伴奏版… 等价于 meta.json 的 version 字段")
    p_imp.add_argument("--replace", action="store_true",
                       help="若库中已有同名同歌手曲目，先删除旧条目再导入")
    p_imp.add_argument("--refresh", action="store_true",
                       help="导入后若 Music 还没索引封面，自动重启音乐强制重读（会短暂关掉音乐）")

    p_ref = sub.add_parser("refresh", help="重启音乐，强制它重读文件标签里的封面")
    p_ref.add_argument("--title", default="", help="顺便复查库中含该关键词的曲目封面数")

    p_find = sub.add_parser("find", help="在资料库中查找曲目")
    p_find.add_argument("--title", required=True)

    args = ap.parse_args()

    if args.cmd == "find":
        try:
            out = osascript(FIND_TMPL % {"title": esc(args.title)}, timeout=60)
        except OSError as exc:
            fail(f"查询 Music 失败：{exc}")
        rows = [r.split("|") for r in out.splitlines() if "|" in r]
        emit({"ok": True, "matches": [
            {"db_id": r[0], "name": r[1], "artist": r[2], "album": r[3],
             "location": r[4] if len(r) > 4 else ""} for r in rows
        ]})
        return

    if args.cmd == "refresh":
        before = artwork_counts(args.title) if args.title else {}
        log("重启音乐…")
        note = restart_music()
        after = artwork_counts(args.title) if args.title else {}
        emit({"ok": True, "action": note, "artworks_before": before, "artworks_after": after})
        return

    path = Path(args.file).expanduser()
    if not path.exists():
        fail(f"文件不存在：{path}")
    meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    if args.version:
        meta["version"] = args.version
    meta = apply_version(meta)      # 翻唱版/剧情版等后缀并进 title，与 meta.py tag 保持一致

    if args.replace:
        want = meta.get("title", path.stem)
        # 带版本后缀的条目只能精确命中；否则重导「载梦行舟」会把
        # 「载梦行舟（翻唱版）」一起当重复删掉
        exact_only = bool(normalize_version(meta.get("version", "")))
        try:
            out = osascript(FIND_TMPL % {"title": esc(want)}, timeout=60)
            for row in out.splitlines():
                parts = row.split("|")
                if len(parts) < 3 or parts[2].strip() != (meta.get("artist") or "").strip():
                    continue
                name = parts[1].strip()
                hit = name == want if exact_only else (
                    name == want or (want in name and "（" not in name))
                if not hit:
                    log(f"  跳过不同版本：{name}")
                    continue
                log(f"  删除旧条目 {name} ({parts[0]})")
                osascript(
                    'tell application "Music" to delete (first track of library playlist 1 '
                    f'whose database ID is {parts[0].strip()})', timeout=60)
        except OSError as exc:
            log(f"  查重失败（继续导入）：{exc}")

    script = IMPORT_TMPL % {"path": esc(str(path)), "fields": build_fields(meta)}
    try:
        dbid = osascript(script, timeout=120)
    except OSError as exc:
        msg = str(exc)
        # 没有「自动化」权限时降级：用 open -a 让音乐把文件加进资料库。
        # 标签已经在文件层面写好，音乐导入时会读取，所以元数据基本不会丢，
        # 代价是没法回读核对、也没法补写封面。
        try:
            cp = subprocess.run(["/usr/bin/open", "-a", "Music", str(path)],
                                capture_output=True, text=True, timeout=60)
            if cp.returncode == 0:
                emit({
                    "ok": True,
                    "db_id": None,
                    "file": str(path),
                    "import_mode": "open-a",
                    "verified": False,
                    "note": ("缺少「音乐」的脚本控制权限，已用 open -a 导入。"
                             "元数据来自文件标签，应已生效，但本次无法回读核对，"
                             "也没法补写封面。授权「自动化 → 音乐」后重跑可获得完整校验。"),
                    "error_detail": msg,
                })
                return
            raise OSError(cp.stderr.strip() or "open -a 失败")
        except Exception:
            pass

        hint = ""
        if "-10004" in msg or "权限违例" in msg:
            hint = ("需要在「系统设置 > 隐私与安全性 > 自动化」中允许本程序控制「音乐」，"
                    "并授予辅助功能权限。")
        elif "-1728" in msg:
            hint = "请先打开「音乐」应用。"
        fail(f"导入 Music 失败：{msg}", hint=hint)

    time.sleep(1)

    # 封面：文件标签里通常已有，若 Music 没读到再显式写入
    cover_note = ""
    if args.cover and Path(args.cover).exists():
        try:
            osascript(COVER_TMPL % {"dbid": dbid.strip(), "cover": esc(str(Path(args.cover).absolute()))},
                      timeout=120)
        except OSError as exc:
            cover_note = f"通过 artwork 属性写入失败（不影响文件标签，可忽略）：{exc}"

    # Music 不会立刻索引新导入文件的 APIC，artworks 会一直是 0；
    # --refresh 会重启一次强制重读 —— 实测唯一可靠的办法
    title = meta.get("title", "")
    art = artwork_counts(title) if title else {}
    refreshed = False
    if args.refresh and not art.get(title, 0):
        log("  封面还没被索引，重启音乐强制重读…")
        restart_music()
        art = artwork_counts(title)
        refreshed = True

    try:
        tags = readback(dbid.strip())
    except OSError as exc:
        tags = {"error": str(exc)}

    emit({
        "ok": True,
        "db_id": dbid.strip(),
        "file": str(path),
        "music_tags": tags,
        "artworks": art,
        "music_restarted": refreshed,
        "cover_note": cover_note or None,
    })


if __name__ == "__main__":
    main()
