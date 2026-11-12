#!/usr/bin/env python3
"""音频转换：FLAC → Apple Lossless（ALAC），任意格式 → MP3 320kbps。

引擎：
  musicconverter  用 Music Converter.app 界面转换（用户指定的首选方式，仅 ALAC）
  afconvert       系统自带 CoreAudio 转换（ALAC）
  ffmpeg          LAME 编码 MP3，CBR 320kbps（macOS 自带的 afconvert 只能解码 MP3，
                  不能编码，所以 MP3 必须走 ffmpeg/lame）

Music Converter 没有 AppleScript 字典，因此走「open -a 导入文件 + System Events 点按钮」，
界面细节随版本变化用 ui_probe.py 重新抓。任一环节失败会自动降级到 afconvert，
并在结果中用 engine 字段说明实际使用的引擎，不会让流程卡死。

用法：
  convert.py run --file in.flac [--format auto|alac|mp3] [--bitrate 320] [--out out.m4a]
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import time
from pathlib import Path

from _common import AUDIO_EXTS, audio_kind, emit, fail, log, osascript, session_dir

# 无损源才值得转 ALAC；有损源转 ALAC 只是换封装、白白变大
LOSSLESS_KINDS = {"flac", "pcm", "alac"}

MC_NAME = "Music Converter"
# 转换产物可能落盘的目录
OUT_HUNT_DIRS = [Path.home() / "Music", Path.home() / "Downloads",
                 Path.home() / "Desktop", Path.home() / "Documents"]

FFMPEG = shutil.which("ffmpeg") or next(
    (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if Path(p).exists()), ""
)


# ---------------------------------------------------------------- 通用

def codec_of(path: Path) -> str:
    try:
        from mutagen import File as MutaFile
        f = MutaFile(str(path))
        return getattr(f.info, "codec", "") if f else ""
    except Exception:
        return ""


def is_alac(path: Path) -> bool:
    """确认产物真的是 ALAC，而不是 AAC 或其它伪装。"""
    if path.suffix.lower() not in {".m4a", ".mp4"}:
        return False
    return codec_of(path).lower() == "alac"


def find_output(src: Path, since: float, max_wait: int = 300) -> Path | None:
    """等待并定位转换产物：优先源目录，其次常见输出目录。"""
    stems = {src.stem, src.stem + " 1"}
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(2)
        search_dirs = [src.parent] + OUT_HUNT_DIRS
        for d in search_dirs:
            if not d.exists():
                continue
            try:
                for p in d.glob("*.m4a"):
                    if p.stem in stems and p.stat().st_mtime >= since - 2:
                        # 体积稳定才算写完
                        s1 = p.stat().st_size
                        time.sleep(1.5)
                        if p.stat().st_size == s1 and s1 > 1024:
                            return p
            except OSError:
                continue
    return None


# ---------------------------------------------------------------- MP3（ffmpeg / LAME）

def is_mp3_ok(path: Path, bitrate: int = 320) -> bool:
    """确认产物真是 MP3 且码率达到要求。"""
    if path.suffix.lower() != ".mp3":
        return False
    try:
        from mutagen.mp3 import MP3
        info = MP3(str(path)).info
        return info.bitrate >= (bitrate - 16) * 1000
    except Exception:
        return False


def encode_mp3(src: Path, out: Path, bitrate: int = 320) -> Path:
    """任意音频/视频 → MP3 CBR（默认 320kbps，MP3 的最高品质）。

    先取第一条音轨再编码，视频容器（mp4/flv）也能直接处理；
    不改变采样率与声道，避免多余的重采样损失。
    """
    if not FFMPEG:
        raise OSError("找不到 ffmpeg（brew install ffmpeg），无法编码 MP3")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-map", "0:a:0", "-vn",
        "-c:a", "libmp3lame", "-b:a", f"{bitrate}k",
        "-id3v2_version", "3",       # 2.3 兼容性最好，Music/iTunes 都能读
        "-write_id3v1", "1",
        str(out),
    ]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if cp.returncode != 0 or not out.exists():
        raise OSError((cp.stderr or cp.stdout or "ffmpeg 编码失败").strip()[:500])
    return out


def probe(path: Path) -> dict:
    """回读关键参数，用于报告实际音质而不是只报「成功」。"""
    try:
        from mutagen import File as MutaFile
        f = MutaFile(str(path))
        if not f:
            return {}
        return {
            "codec": getattr(f.info, "codec", ""),
            "bitrate": getattr(f.info, "bitrate", 0),
            "sample_rate": getattr(f.info, "sample_rate", 0),
            "channels": getattr(f.info, "channels", 0),
            "duration": round(getattr(f.info, "length", 0), 1),
        }
    except Exception:
        return {}


# ---------------------------------------------------------------- afconvert

def convert_afconvert(src: Path, out: Path) -> Path:
    """CoreAudio：任意受支持输入 -> ALAC in m4a。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    cp = subprocess.run(
        ["/usr/bin/afconvert", "-f", "m4af", "-d", "alac", str(src), str(out)],
        capture_output=True, text=True, timeout=1800,
    )
    if cp.returncode != 0 or not out.exists():
        raise OSError((cp.stderr or cp.stdout or "afconvert 失败").strip())
    return out


# ---------------------------------------------------------------- Music Converter

# 在 Music Converter 界面里点「转换」：按钮文字随语言/版本不同，做多重匹配
MC_CLICK_SCRIPT = '''
tell application "%(app)s" to activate
delay 1
tell application "System Events"
  tell process "%(proc)s"
    set frontmost to true
    delay 0.5
    -- 1) 先确保输出格式是 Apple Lossless / ALAC
    try
      set popups to every pop up button of window 1
      repeat with p in popups
        try
          click p
          delay 0.3
          set items_ to name of every menu item of menu 1 of p
          repeat with target in {"Apple Lossless", "ALAC", "Apple Lossless (ALAC)", "苹果无损"}
            if items_ contains (target as text) then
              click menu item (target as text) of menu 1 of p
              exit repeat
            end if
          end repeat
          try
            key code 53
          end try
        end try
      end repeat
    end try
    -- 2) 点转换按钮
    set btns to every button of window 1
    repeat with b in btns
      try
        set nm to (name of b) as text
        set ds to ""
        try
          set ds to (description of b) as text
        end try
        if nm contains "Convert" or nm contains "转换" or ds contains "Convert" or ds contains "转换" then
          if (enabled of b) is true then
            click b
            return "clicked:" & nm
          end if
        end if
      end try
    end repeat
    return "nobutton"
  end tell
end tell
'''


def convert_musicconverter(src: Path) -> Path:
    """用 Music Converter 界面转换，返回产物路径。"""
    if not Path("/Applications/Music Converter.app").exists():
        raise OSError("未安装 Music Converter")

    since = time.time()
    log("  用 Music Converter 打开文件…")
    cp = subprocess.run(["/usr/bin/open", "-a", "Music Converter", str(src)],
                        capture_output=True, text=True, timeout=60)
    if cp.returncode != 0:
        raise OSError(f"无法用 Music Converter 打开文件：{cp.stderr.strip()}")

    time.sleep(4)  # 等界面把文件读进列表

    clicked = osascript(MC_CLICK_SCRIPT % {"app": MC_NAME, "proc": MC_NAME}, timeout=120)
    log(f"  界面操作结果：{clicked}")
    if clicked == "nobutton":
        raise OSError("界面上找不到转换按钮（可能是界面语言/版本差异）")

    out = find_output(src, since, max_wait=600)
    if out is None:
        raise OSError("转换后未找到输出文件")
    return out


# ---------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser(description="音频转换（ALAC / MP3 320）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="转换单个文件")
    p.add_argument("--file", required=True)
    p.add_argument("--format", default="auto", choices=["auto", "alac", "mp3"],
                   help="auto：无损源转 ALAC、有损源转 MP3（默认）")
    p.add_argument("--bitrate", type=int, default=320, help="MP3 码率，默认 320")
    p.add_argument("--engine", default="auto",
                   choices=["auto", "musicconverter", "afconvert", "ffmpeg"],
                   help="ALAC 的转换引擎；MP3 固定用 ffmpeg（afconvert 不能编码 MP3）")
    p.add_argument("--out", default="", help="输出路径")
    p.add_argument("--slug", default="", help="任务名")
    args = ap.parse_args()

    src = Path(args.file).expanduser()
    if not src.exists():
        fail(f"文件不存在：{src}")

    fmt = args.format
    if fmt == "auto":
        kind = audio_kind(src)
        # 无损源才有必要保留无损；B 站这类有损源转 ALAC 只会白白变大
        fmt = "alac" if kind in LOSSLESS_KINDS else "mp3"
        log(f"源格式 {kind} → 目标格式 {fmt}")

    if fmt == "mp3":
        out = Path(args.out).expanduser() if args.out else src.with_suffix(".mp3")
        if is_mp3_ok(src, args.bitrate):
            emit({"ok": True, "file": str(src), "engine": "none",
                  "note": "已是达标 MP3，跳过转换", **probe(src)})
            return
        try:
            produced = encode_mp3(src, out, args.bitrate)
        except (OSError, subprocess.SubprocessError) as exc:
            fail(f"MP3 编码失败：{exc}", hint="确认已安装 ffmpeg（brew install ffmpeg）")
        info = probe(produced)
        if not is_mp3_ok(produced, args.bitrate):
            fail(f"产物码率不达标：{info.get('bitrate', 0) // 1000} kbps")
        emit({"ok": True, "file": str(produced), "engine": "ffmpeg",
              "format": "mp3", "size": produced.stat().st_size, **info})
        return

    # ---- ALAC ----
    if is_alac(src):
        emit({"ok": True, "file": str(src), "engine": "none", "note": "已是 ALAC，跳过转换"})
        return

    out = Path(args.out).expanduser() if args.out else src.with_suffix(".m4a")

    engine = args.engine
    errors: list[str] = []

    if engine in {"auto", "musicconverter"}:
        try:
            log("尝试 Music Converter…")
            produced = convert_musicconverter(src)
            # 统一整理到目标路径
            if produced.resolve() != out.resolve():
                out.parent.mkdir(parents=True, exist_ok=True)
                if out.exists():
                    out.unlink()
                produced.replace(out)
            if not is_alac(out):
                raise OSError(f"产物不是 ALAC（codec={codec_of(out) or '未知'}）")
            emit({"ok": True, "file": str(out), "engine": "musicconverter",
                  "codec": codec_of(out), "size": out.stat().st_size})
            return
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"Music Converter: {exc}")
            log(f"  Music Converter 失败：{exc}")
            if engine == "musicconverter":
                fail("Music Converter 转换失败", errors=errors)

    if engine in {"auto", "afconvert", "ffmpeg"}:
        try:
            log("使用 afconvert（CoreAudio）转换…")
            produced = convert_afconvert(src, out)
            emit({"ok": True, "file": str(produced), "engine": "afconvert",
                  "codec": codec_of(produced), "size": produced.stat().st_size,
                  "fallback_reason": errors or None})
            return
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"afconvert: {exc}")

    fail("转换失败", errors=errors)


if __name__ == "__main__":
    main()
