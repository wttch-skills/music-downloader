#!/usr/bin/env python3
"""通过 Downie 4 下载，并等待完成。

Downie 的脚本字典只有两个命令：
  open all URLs in text "<文本>"   -> 1 表示识别到链接并加入队列，0 表示没找到链接
  start queue                      -> 1 已启动 / 0 已在运行 / -1 队列为空

它不暴露查询队列状态的命令，也无法指定保存目录，所以这里用三重手段判定完成：
  1. 下载目录快照 diff，找出新出现的文件
  2. 该文件大小连续若干秒不变
  3. Downie 内部队列 plist 中不再有活动项（辅助判断）

用法：
  downie.py download --url <URL> [--slug <任务名>]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from _common import (AUDIO_EXTS, TMP_ROOT, VIDEO_EXTS, emit, fail, log,
                     session_dir)

QUEUE_PLIST = Path.home() / "Library/Containers/com.charliemonroe.Downie-4/Data/Library/Preferences/com.charliemonroe.Downie-4.plist"

# Downie 可能落盘的目录（沙盒应用 + 用户自选目录都覆盖）
WATCH_DIRS = [
    Path.home() / "Downloads",
    Path.home() / "Library/Containers/com.charliemonroe.Downie-4/Data/Downloads",
    TMP_ROOT,          # 用户在 Downie 里把下载目录设成临时目录时，直接落在这一层
    Path.home() / "Music",
    Path.home() / "Movies",
    Path.home() / "Desktop",
]

MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS
PARTIAL_SUFFIX = {".download", ".part", ".downloadpart", ".crdownload", ".tmp"}

# Downie 4 的 XUPostprocessing 取值（实测）：0=无后处理，2=仅提取音频
POSTPROCESSING_AUDIO_ONLY = 2
POSTPROCESSING_NONE = 0
# 后处理是下载完成后再跑一遍编码，视频落地到音频出现之间需要额外等待
POSTPROCESS_GRACE = 180
# Downie 分片下载时会往目标目录写 UUID 命名的中间文件，不能当成品
UUID_NAME = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\.")
# 分片下载的片段：xxx.01.mp3 / xxx.02.m4a，最终会合并成完整文件
SEGMENT_NAME = re.compile(r"\.\d{2,3}\.(mp3|m4a|flac|mp4|aac|wav|m4v)$", re.IGNORECASE)


def snapshot() -> dict[str, tuple[int, float]]:
    """记录监控目录下媒体文件的 (大小, mtime)。只扫两层，够用且快。"""
    state: dict[str, tuple[int, float]] = {}
    for base in WATCH_DIRS:
        if not base.exists():
            continue
        try:
            entries = list(os.scandir(base))
        except OSError:
            continue
        for e in entries:
            if e.is_file():
                _add(state, e)
            elif e.is_dir() and not e.name.startswith("."):
                try:
                    for f in os.scandir(e.path):
                        if f.is_file():
                            _add(state, f)
                except OSError:
                    continue
    return state


def _add(state: dict, entry: os.DirEntry) -> None:
    p = Path(entry.path)
    if p.suffix.lower() in PARTIAL_SUFFIX:
        return
    if p.suffix.lower() not in MEDIA_EXTS:
        return
    if UUID_NAME.match(p.name) or p.name.startswith("."):
        return
    if SEGMENT_NAME.search(p.name):
        return
    try:
        st = entry.stat()
    except OSError:
        return
    state[str(p)] = (st.st_size, st.st_mtime)


def clean_name(name: str) -> str:
    """Downie 的默认文件名模板是「标题 - 序号 - 标题 - 时间戳_0.ext」，
    收敛成标题。模板固定形如 `<title> - 001 - <title>`，所以直接截到序号前那一段。
    """
    suffix = Path(name).suffix
    stem = Path(name).stem
    stem = re.sub(r"_\d+$", "", stem)  # 末尾 _0

    m = re.match(r"^(.+?)\s+-\s+0*\d+\s+-\s+", stem)
    if m:
        return m.group(1).strip(" -") + suffix

    # 没有序号段的（比如单文件直链），只清时间戳
    stem = re.sub(r"\s*-\s*\d{4}-\d{2}-\d{2}\s+\d{2}-\d{2}-\d{2}\s*$", "", stem)
    stem = re.sub(r"\s*-\s*-\s*$", "", stem)
    return (re.sub(r"\s+", " ", stem).strip(" -") or Path(name).stem) + suffix


def queue_active() -> bool:
    """读 Downie 的队列 plist，判断是否还有未完成任务。读不到时返回 False（不阻塞流程）。"""
    try:
        with QUEUE_PLIST.open("rb") as fh:
            data = plistlib.load(fh)
    except Exception:
        return False
    for key, value in data.items():
        if "XUDownloadsController.Queue" in key and "processing" in key:
            if isinstance(value, list) and value:
                return True
    return False


def _read_default(key: str) -> str:
    cp = subprocess.run(["/usr/bin/defaults", "read", "com.charliemonroe.Downie-4", key],
                        capture_output=True, text=True)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def _read_autostart() -> bool:
    """读 Downie 的「加入队列后自动开始」偏好。"""
    return _read_default("XUAutomaticallyStartsDownloads") == "1"


def _postprocessing_mode() -> int | None:
    try:
        return int(_read_default("XUPostprocessing"))
    except ValueError:
        return None


def ensure_prefs() -> list[str]:
    """让 Downie 在下载阶段就把「仅提取音频 + MP3 320kbps」做掉。

    Downie 4 的后处理偏好只有一个键 XUPostprocessing，实测取值：
      0 = 无后处理   2 = 仅提取音频（配合音频格式偏好输出 MP3 320kbps）
    设为 2 后它会在下载完成后自动抽出音轨、按偏好编码成 MP3（实测 320kbps CBR、
    44.1kHz 立体声），因此不需要我们自己再转一次。

    start queue 只能靠 AppleScript（需 TCC 授权），所以改成打开「自动开始下载」，
    用 URL scheme 投递即可全自动。返回本次改了哪些项。
    """
    changed: list[str] = []

    if not _read_autostart():
        subprocess.run(["/usr/bin/defaults", "write", "com.charliemonroe.Downie-4",
                        "XUAutomaticallyStartsDownloads", "-bool", "true"],
                       capture_output=True, text=True)
        changed.append("自动开始下载 → 开启")

    # 「下载前确认目录」会让每次下载弹出保存面板，无人值守时整条流程会卡死
    if _read_default("XUAskForDestination") == "1":
        subprocess.run(["/usr/bin/defaults", "write", "com.charliemonroe.Downie-4",
                        "XUAskForDestination", "-bool", "false"],
                       capture_output=True, text=True)
        changed.append("下载前确认目录 → 关闭（否则无人值守会卡在保存面板）")

    if _postprocessing_mode() != POSTPROCESSING_AUDIO_ONLY:
        subprocess.run(["/usr/bin/defaults", "write", "com.charliemonroe.Downie-4",
                        "XUPostprocessing", "-int", str(POSTPROCESSING_AUDIO_ONLY)],
                       capture_output=True, text=True)
        changed.append("后处理 → 仅提取音频（输出 MP3）")

    if not changed:
        return changed

    log("已调整 Downie 设置：" + "；".join(changed) + "（只需要一次）")
    # 偏好改动需要重启 Downie 才生效；此时队列里通常没有东西，代价可接受
    if subprocess.run(["/usr/bin/pgrep", "-x", "Downie 4"], capture_output=True).returncode == 0:
        log("  重启 Downie 以载入新设置…")
        subprocess.run(["/usr/bin/killall", "Downie 4"], capture_output=True)
        time.sleep(3)
        subprocess.run(["/usr/bin/open", "-a", "Downie 4"], capture_output=True)
        time.sleep(4)
    return changed


def set_postprocessing(mode: int) -> bool:
    """把 XUPostprocessing 设成指定值并重启 Downie 让它生效。返回是否真的改了。

    只有下载 Hi-Res / 无损源时才需要临时关掉（0），否则 Downie 会把音轨统一编码成
    320kbps MP3，原始采样率和码率就丢了。
    """
    if _postprocessing_mode() == mode:
        return False
    subprocess.run(["/usr/bin/defaults", "write", "com.charliemonroe.Downie-4",
                    "XUPostprocessing", "-int", str(mode)], capture_output=True, text=True)
    if subprocess.run(["/usr/bin/pgrep", "-x", "Downie 4"], capture_output=True).returncode == 0:
        subprocess.run(["/usr/bin/killall", "Downie 4"], capture_output=True)
        time.sleep(3)
        subprocess.run(["/usr/bin/open", "-a", "Downie 4"], capture_output=True)
        time.sleep(4)
    return True


def send_to_downie(url: str, ensure: bool = True) -> int:
    """用 Downie 的 URL scheme 投递，不经过 AppleScript，因此不需要 TCC 授权。

    Downie 注册了 downie:// scheme，`XUOpenURL` action 等价于把链接拖进窗口。
    返回 1 表示已投递（scheme 投递无法感知识别失败，靠后续轮询判断）。
    """
    if ensure:
        ensure_prefs()
    encoded = quote(url, safe="")
    cp = subprocess.run(["/usr/bin/open", f"downie://XUOpenURL?url={encoded}"],
                        capture_output=True, text=True, timeout=60)
    if cp.returncode != 0:
        raise OSError(f"投递到 Downie 失败：{cp.stderr.strip()}")
    return 1


def wait_for_download(before: dict, timeout: int = 900, grace: int = POSTPROCESS_GRACE) -> list[Path]:
    """轮询等待新文件出现并稳定。

    开了「仅提取音频」后处理时，Downie 可能先把视频落盘、再编码出音频，
    所以音频文件优先；若当前只拿到视频，会再等一段后处理宽限期。
    grace=0 表示不做后处理（要原始音轨），不用等音频。
    """
    deadline = time.time() + timeout
    candidate: Path | None = None
    last_size = -1
    stable_since = 0.0
    quiet_rounds = 0
    grace_until = 0.0

    while time.time() < deadline:
        time.sleep(3)
        after = snapshot()
        new_keys = [k for k in after if k not in before]

        if new_keys:
            # 音频优先，其次取最新的；Downie 可能顺带写封面、字幕等小文件
            audio_new = [k for k in new_keys if Path(k).suffix.lower() in AUDIO_EXTS]
            pool = audio_new or new_keys
            newest = max(pool, key=lambda k: after[k][1])
            p = Path(newest)
            size = after[newest][0]
            if p != candidate:
                if candidate is not None and candidate in before:
                    # 之前抓到的是视频，现在换成音频，说明后处理完成了
                    grace_until = 0.0
                candidate, last_size, stable_since = p, size, time.time()
                log(f"  发现新文件：{p.name}（{size // 1024} KB）")
                continue
            if size != last_size:
                last_size, stable_since = size, time.time()
                continue
            # 大小稳定 6 秒且队列空闲，认为下载结束
            if time.time() - stable_since >= 6:
                if queue_active():
                    quiet_rounds = 0
                    continue
                # 视频落地后可能还在转码，给它一个宽限期等音频出现
                if candidate.suffix.lower() in VIDEO_EXTS and grace > 0:
                    if not grace_until:
                        grace_until = time.time() + grace
                        log("  视频已落盘，等待后处理产出音频…")
                        continue
                    if time.time() < grace_until:
                        continue
                    log("  后处理超时，改用原始视频（稍后可用 convert.py 转 MP3）")
                quiet_rounds += 1
                if quiet_rounds >= 1:
                    return [candidate]

    return [candidate] if candidate else []


DOWNIE_APP = "/Applications/Downie 4.app"


def _downie_running() -> bool:
    return subprocess.run(["/usr/bin/pgrep", "-x", "Downie 4"],
                          capture_output=True).returncode == 0


def _quit_downie(wait: int = 12) -> bool:
    if not _downie_running():
        return True
    subprocess.run(["/usr/bin/killall", "Downie 4"], capture_output=True)
    for _ in range(wait * 2):
        time.sleep(0.5)
        if not _downie_running():
            return True
    return False


def _launch_downie() -> None:
    subprocess.run(["/usr/bin/open", "-a", "Downie 4"], capture_output=True)


def ensure_dest(restart_if_needed: bool = True) -> dict:
    """让 Downie 自动下载进临时目录，且不再弹「确认每次下载的目的地」。

    Downie 是沙盒应用：只有用户在原生文件对话框里亲手选过的目录才有写权限。
    它「设置 > 下载目录 > 下载文件保存至文件夹」是个自定义 NSMenu，点开就会让
    Downie 主线程进入模态跟踪，所有辅助功能查询立刻卡死（实测，读不到菜单项）。
    所以走两条稳定路线：
      1) 把容器里的 Downloads 做成指向临时目录的软链接
         （Downie 以为自己在下到「下载」，实际落在工作目录的 tmp/music）
      2) 关掉偏好键 XUAskForDestination（= 设置界面里的「确认每次下载的目的地」）
    两条都幂等；dest --probe 可以去设置界面核对真实状态。
    """
    from _common import (downie_dest_source, downie_destination,
                         downie_link_downloads, downie_pref, downie_set)

    out: dict = {}

    try:
        downie_link_downloads(TMP_ROOT)
        out["symlink"] = f"容器 Downloads → {TMP_ROOT}"
    except OSError as exc:
        out["symlink"] = f"软链接失败：{exc}"

    if not restart_if_needed:
        out["ask"] = "已跳过"
        out["source"] = downie_dest_source()
        out["effective"] = downie_destination()
        return out

    cur = downie_pref("XUAskForDestination")
    if cur in {"1", "true", "TRUE", "YES", "yes"}:
        log("Downie 现在每次下载都会弹「确认目的地」，关掉它…")
        if not _quit_downie():
            out["ask"] = "关闭失败：Downie 没退出，手动退出后重试"
        else:
            downie_set("XUAskForDestination", "-bool", "false")
            _launch_downie()
            time.sleep(3)
            out["ask"] = "已关闭「确认每次下载的目的地」（已重启 Downie 生效）"
        out["restarted"] = True
    else:
        out["ask"] = "不用改：本来就不弹确认"

    out["ask_value"] = downie_pref("XUAskForDestination")
    out["source"] = downie_dest_source()
    out["effective"] = downie_destination()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Downie 4 下载器")
    ap.add_argument("cmd", nargs="?", default="download", choices=["download", "dest"],
                    help="download 下载；dest 查看/设置 Downie 的下载落盘目录")
    ap.add_argument("--url", default="", help="要下载的页面或媒体地址")
    ap.add_argument("--slug", default="", help="任务名，用于隔离临时目录")
    ap.add_argument("--timeout", type=int, default=900, help="最长等待秒数")
    ap.add_argument("--format", default="mp3", choices=["mp3", "original"],
                    help="mp3：要求产出最高品质 MP3（默认，靠 Downie 后处理）；"
                         "original：保留原始音轨，会临时关掉 Downie 后处理，"
                         "用于 Hi-Res / 无损源（否则会被压成 320kbps MP3）")
    ap.add_argument("--bitrate", type=int, default=320, help="本地补转时的 MP3 码率")
    ap.add_argument("--no-wait", action="store_true", help="只投递不等待")
    ap.add_argument("--keep-dest", action="store_true",
                    help="不改 Downie 的下载目录（默认会确保它指向临时目录）")
    ap.add_argument("--probe", action="store_true",
                    help="配合 dest：只 dump Downie 设置窗口的 UI 结构（不改任何设置）")
    ap.add_argument("--restore-dest", action="store_true",
                    help="配合 dest：还原成默认的 ~/Downloads")
    args = ap.parse_args()

    if not Path("/Applications/Downie 4.app").exists():
        fail("未安装 Downie 4")

    from _common import downie_dest_source, downie_destination

    if args.cmd == "dest":
        if args.probe:
            from _common import downie_dump_ui
            log("读取 Downie「设置 > 下载目录」页面（只读，不改任何设置）…")
            res = downie_dump_ui(mode="dumpdest", timeout=120)
            if not res.get("ok"):
                fail("读取设置界面失败",
                     hint=res.get("detail", ""),
                     needs_accessibility=res.get("needs_accessibility", False))
            controls = [e for e in res["tree"]
                        if e["win"] == "1"
                        and e["role"] in {"AXCheckBox", "AXMenuButton", "AXPopUpButton"}]
            ask = next((c for c in controls if "目的地" in c["name"]), None)
            emit({"ok": True,
                  "pane": res.get("sel"),
                  "windows": [w["name"] for w in res["windows"]],
                  "ask_for_destination": ask["value"] if ask else "未找到",
                  "controls": controls})
            return

        if args.restore_dest:
            from _common import downie_restore_downloads
            try:
                downie_restore_downloads()
            except OSError as exc:
                fail(f"还原下载目录失败：{exc}")
            emit({"ok": True, "action": "容器 Downloads 已还原成真实目录",
                  "source": downie_dest_source(),
                  "effective": str(Path.home() / "Downloads")})
            return

        res = ensure_dest(restart_if_needed=True)
        emit({"ok": True, "action": "下载已指向临时目录，且不再弹确认面板", **res})
        return

    if not args.url:
        fail("缺少 --url")

    # 让 Downie 自动把文件写进临时目录，且不弹「确认目的地」（详见 ensure_dest）
    if not args.keep_dest:
        prep = ensure_dest(restart_if_needed=True)
        log(f"下载落盘：{prep.get('effective')}（{prep.get('source')}）")
        log("  " + str(prep.get("ask", "")))

    slug = args.slug or hashlib.md5(args.url.encode()).hexdigest()[:8]
    workdir = session_dir(slug)

    before = snapshot()

    # 要保留原始音轨（Hi-Res / 无损源）时，必须先关掉 Downie 的「仅提取音频」后处理，
    # 否则它统一编码成 320kbps MP3，源本身的采样率和码率就丢了。
    restore_pp: int | None = None
    if args.format == "original":
        prev = _postprocessing_mode()
        if prev != POSTPROCESSING_NONE:
            log("保留原始音轨：临时关闭 Downie 的「仅提取音频」后处理…")
            set_postprocessing(POSTPROCESSING_NONE)
            restore_pp = prev

    log(f"投递到 Downie：{args.url}")
    try:
        result = send_to_downie(args.url, ensure=args.format != "original")
    except OSError as exc:
        if restore_pp is not None:
            set_postprocessing(restore_pp)
        fail(f"调用 Downie 失败：{exc}")

    if result == 0:
        fail("Downie 没有从该地址识别到可下载的媒体", url=args.url)

    if args.no_wait:
        emit({"ok": True, "queued": True, "url": args.url})
        return

    log("已加入队列，等待下载完成…")
    files = wait_for_download(before, args.timeout,
                              grace=0 if args.format == "original" else POSTPROCESS_GRACE)
    if restore_pp is not None:
        log("下载完成，恢复 Downie 的后处理设置…")
        set_postprocessing(restore_pp)
    if not files:
        fail("等待超时，未发现新文件。",
             url=args.url,
             hint="常见原因：① Downie 卡在解析或站点需要登录；② 「下载前确认目录」被打开，"
                  "下载卡在保存面板上（脚本会自动关闭该选项并重启 Downie，可重试一次）；"
                  "③ 下载目录被设成了脚本没监控的位置，可在 doctor.py 里看「Downie 下载目录」")

    path = files[0]
    # 把成品挪到任务目录，避免污染用户下载目录
    target = workdir / clean_name(path.name)
    try:
        if path.resolve() != target.resolve():
            path.replace(target)
            path = target
    except OSError as exc:
        log(f"  移动文件失败（继续使用原路径）：{exc}")

    # 兜底：Downie 的后处理没生效时，本地补一次 MP3 320 编码
    converted = None
    if args.format == "mp3" and path.suffix.lower() != ".mp3":
        try:
            from convert import encode_mp3
            log("  下载产物不是 MP3，本地补转 320kbps…")
            path = encode_mp3(path, path.with_suffix(".mp3"), args.bitrate)
            converted = "local-ffmpeg"
        except Exception as exc:
            log(f"  本地补转失败（保留原文件）：{exc}")

    emit({
        "ok": True,
        "file": str(path),
        "size": path.stat().st_size,
        "ext": path.suffix.lower(),
        "workdir": str(workdir),
        "postprocessing": "downie" if converted is None and path.suffix.lower() == ".mp3" else converted,
    })


if __name__ == "__main__":
    main()
