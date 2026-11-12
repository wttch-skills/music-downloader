#!/usr/bin/env python3
"""环境自检：一次性列出所有前置条件的状态和修复方法。

在正式跑流程前先执行，避免中途卡在权限或依赖问题上。

用法：
  doctor.py
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from _common import (DOWNIE_APP, MUSIC_APP, MUSIC_CONVERTER_APP, TMP_ROOT,
                     WORKSPACE, emit, osascript, run)

CHECKS: list[dict] = []


def add(name: str, ok: bool, detail: str, fix: str = "", blocking: bool = True) -> None:
    CHECKS.append({"item": name, "ok": ok, "detail": detail,
                   "blocking": blocking, "fix": fix if not ok else ""})


def check_python_deps() -> None:
    missing = []
    for mod in ("mutagen", "requests", "bs4"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    add("Python 依赖", not missing,
        "mutagen / requests / bs4 就绪" if not missing else f"缺少：{', '.join(missing)}",
        "python3 -m pip install mutagen requests beautifulsoup4 lxml")


def check_apps() -> None:
    for label, path in (("Downie 4", DOWNIE_APP), ("Music Converter", MUSIC_CONVERTER_APP),
                        ("Apple Music", MUSIC_APP)):
        p = Path(path)
        add(label, p.exists(), str(p), f"请安装 {label}")


def check_downie() -> None:
    """Downie 走 URL scheme 投递，不需要 AppleScript 自动化权限。
    重点检查两个偏好：「自动开始下载」和「后处理 = 仅提取音频（输出 MP3）」。"""
    add("open 命令", Path("/usr/bin/open").exists(), "/usr/bin/open")

    def pref(key: str) -> str:
        cp = subprocess.run(["/usr/bin/defaults", "read", "com.charliemonroe.Downie-4", key],
                            capture_output=True, text=True)
        return cp.stdout.strip() if cp.returncode == 0 else ""

    autostart = pref("XUAutomaticallyStartsDownloads") == "1"
    add("Downie 自动开始下载", autostart,
        "已开启" if autostart else "未开启（首次下载时脚本会自动打开并重启 Downie）",
        blocking=False)  # 脚本能自己修，不算阻塞

    mode = pref("XUPostprocessing")
    audio_only = mode == "2"
    add("Downie 后处理 = 仅提取音频", audio_only,
        "已开启，下载即为 320kbps MP3" if audio_only
        else f"当前值 {mode or '默认(无后处理)'}，下载会先得到视频再补转"
             "（脚本会自动改成 2 并重启 Downie）",
        blocking=False)

    ask = pref("XUAskForDestination")
    add("Downie 下载前确认目录", ask != "1",
        "已关闭（可直接下载）" if ask != "1"
        else "已开启，每次下载会弹保存面板，无人值守会卡住（脚本会自动关掉并重启 Downie）",
        blocking=False)

    # 下载目录：Downie 是沙盒应用，外部写进去的路径拿不到写权限，
    # 所以用容器 Downloads 软链接把落盘位置转到临时目录；dest 命令会自动维护这条链接。
    from _common import downie_dest_source, downie_destination
    dest = downie_destination()
    source = downie_dest_source()
    dest_hint = "python3 <skill>/scripts/downie.py dest   # 自动把落盘目录指到临时目录"
    if dest and Path(dest).resolve() == TMP_ROOT.resolve():
        add("Downie 下载目录", True,
            f"已指向临时目录（{'容器软链接' if source == 'symlink' else source}）：{dest}")
    elif dest:
        add("Downie 下载目录", False,
            f"{dest}（不是临时目录，文件落盘后脚本会自动搬过去）", dest_hint,
            blocking=False)
    else:
        add("Downie 下载目录", False,
            f"默认 ~/Downloads，文件落盘后由脚本搬进 {TMP_ROOT}", dest_hint,
            blocking=False)


def check_mp3_encoder() -> None:
    """MP3 编码兜底：macOS 的 afconvert 不能编码 MP3，必须 ffmpeg/LAME。"""
    import shutil
    ff = shutil.which("ffmpeg") or next(
        (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if Path(p).exists()), "")
    if ff:
        cp = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True)
        has_lame = "libmp3lame" in cp.stdout
        add("MP3 编码器 (ffmpeg/LAME)", has_lame,
            f"{ff}，libmp3lame 可用" if has_lame else f"{ff} 缺少 libmp3lame",
            "brew install ffmpeg")
    else:
        add("MP3 编码器 (ffmpeg/LAME)", False, "未安装 ffmpeg",
            "brew install ffmpeg（不影响主流程：Downie 后处理可直接输出 MP3）",
            blocking=False)


def check_automation() -> None:
    """Music 的脚本控制用于导入后核对元数据。拿不到时降级为 open -a 导入 + 不做回读校验。"""
    try:
        out = osascript('tell application "Music" to return (count of tracks of library playlist 1)',
                        timeout=30)
        add("脚本控制 · 音乐", True, f"已授权（库中 {out} 首）")
    except OSError as exc:
        msg = str(exc)
        if "-10004" in msg or "权限违例" in msg or "not authorized" in msg.lower():
            add("脚本控制 · 音乐", False, msg,
                "系统设置 > 隐私与安全性 > 自动化 → 勾选本程序对「音乐」的控制权。"
                "未授权时仍可用 open -a 导入，但无法自动核对元数据与补写封面",
                blocking=False)
        else:
            add("脚本控制 · 音乐", False, msg, "确认「音乐」能正常启动", blocking=False)


def check_accessibility() -> None:
    """两套 UI 自动化权限要分开看：

    1. DownieDestPicker（独立 app）：负责在 Downie 设置里选下载目录。权限归属是 app 自己，
       所以授权它即可，不需要给 WorkBuddy 授权、也不用重启。
    2. WorkBuddy 自身：负责 Music Converter 界面转换和网盘页面点击，两项都有降级路径。
    """
    from _common import downie_picker_ready

    r = downie_picker_ready()
    add("辅助功能 · Downie 目录选择器", r["ok"], r["detail"],
        "" if r["ok"] else "把 DownieDestPicker 拖进「辅助功能」列表并勾选（app 已在 Finder 中选中）",
        blocking=False)

    try:
        osascript('tell application "System Events" to return name of first process', timeout=25)
        add("辅助功能 · WorkBuddy", True, "已授权（界面转换 / 网盘点击可用）")
    except OSError as exc:
        add("辅助功能 · WorkBuddy", False, str(exc),
            "系统设置 > 隐私与安全性 > 辅助功能 → 添加 WorkBuddy，改完需重启。"
            "未授权时：FLAC→ALAC 走 afconvert（同一编码器）、网盘需手动点",
            blocking=False)


def check_cdp() -> None:
    """Proxy 进程活着不代表连上了浏览器：后端断开时它仍会返回 200 + 纯文本报错，
    所以必须验证 /targets 能解析成标签页列表。"""
    try:
        import requests
        r = requests.get("http://localhost:3456/targets", timeout=8)
        body = r.text.strip()
        try:
            targets = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(body[:120] or "返回非 JSON")
        if not isinstance(targets, list):
            raise RuntimeError(f"返回结构异常：{type(targets).__name__}")
        add("浏览器 CDP", True, f"已连上浏览器，当前 {len(targets)} 个标签页")
    except Exception as exc:
        detail = str(exc)
        if "Connection refused" in detail or "拒绝" in detail:
            detail = "Proxy 未连上浏览器（upstream connection refused）"
        add("浏览器 CDP", False, detail,
            'node "/Users/wttch/.workbuddy/skills/web-access/scripts/check-deps.mjs"\n'
            "  并在 Chrome 打开 chrome://inspect/#remote-debugging，"
            "勾选 Allow remote debugging for this browser instance")


def check_tools() -> None:
    for tool in ("afconvert", "open"):
        p = subprocess.run(["/usr/bin/which", tool], capture_output=True, text=True)
        add(f"系统工具 {tool}", p.returncode == 0, p.stdout.strip() or "未找到")


def check_workspace() -> None:
    try:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        probe = TMP_ROOT / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("临时目录可写", True, str(TMP_ROOT))
    except OSError as exc:
        add("临时目录可写", False, str(exc), "检查工作目录权限")
    add("工作目录", True, str(WORKSPACE))


def main() -> None:
    check_python_deps()
    check_apps()
    check_downie()
    check_mp3_encoder()
    check_automation()
    check_accessibility()
    check_cdp()
    check_tools()
    check_workspace()

    blockers = [c for c in CHECKS if not c["ok"] and c.get("blocking")]
    warnings = [c for c in CHECKS if not c["ok"] and not c.get("blocking")]
    emit({
        "ok": not blockers,
        "ready": not blockers,
        "blockers": len(blockers),
        "warnings": len(warnings),
        "blocked_items": [c["item"] for c in blockers],
        "degraded_items": [c["item"] for c in warnings],
        "checks": CHECKS,
    })


if __name__ == "__main__":
    main()
