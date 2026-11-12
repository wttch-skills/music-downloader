"""music-downloader 共享工具：路径解析、JSON 输出、子进程执行、日志。

所有脚本统一约定：stdout 只输出一行 JSON（便于 Agent 解析），
人可读的进度信息走 stderr。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- 路径

SKILL_DIR = Path(__file__).resolve().parent.parent
# ./scripts/_common.py -> skill 根 -> 工作目录（skill 位于 <workspace>/.workbuddy/skills/<name>）
WORKSPACE = Path(
    os.environ.get("MUSIC_WORKSPACE") or SKILL_DIR.parent.parent.parent
).resolve()

TMP_ROOT = WORKSPACE / "tmp" / "music"

DOWNIE_APP = "/Applications/Downie 4.app"
MUSIC_CONVERTER_APP = "/Applications/Music Converter.app"
MUSIC_APP = "/System/Applications/Music.app"


def ensure_tmp(*parts: str) -> Path:
    """创建并返回工作目录下的临时子目录。"""
    p = TMP_ROOT.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def session_dir(slug: str) -> Path:
    """单次任务的临时目录，按 slug 隔离，避免多次任务互相污染。"""
    return ensure_tmp(slug)


# ---------------------------------------------------------------- 输出

def emit(payload: dict) -> None:
    """输出结构化结果（唯一写到 stdout 的内容）。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fail(msg: str, **extra) -> None:
    """输出失败结果并退出，exit code 仍为 0 以便调用方读取 JSON 判断。"""
    payload = {"ok": False, "error": msg}
    payload.update(extra)
    emit(payload)
    sys.exit(0)


# ---------------------------------------------------------------- 子进程

def run(cmd: list[str] | str, timeout: int = 120, check: bool = False) -> subprocess.CompletedProcess:
    """执行命令，返回 CompletedProcess（文本模式）。"""
    shell = isinstance(cmd, str)
    return subprocess.run(
        cmd,
        shell=shell,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


AS_BRIDGE_SRC = Path("/tmp/wb_applescript_bridge.applescript")
AS_BRIDGE_SCPT = Path("/tmp/wb_applescript_bridge.scpt")


def osascript_via_picker(script: str, timeout: int = 180) -> str:
    """借 DownieDestPicker.app 的 TCC 身份执行 AppleScript。

    WorkBuddy 是 Electron，派生的 `osascript` 在 TCC 里是独立主体，
    控制「音乐」时会直接 -10004（给 WorkBuddy 授权也没用，因为发包的不是它）。
    applet 身份明确，首次控制「音乐」会弹一次「自动化」授权，点允许后长期有效。
    """
    AS_BRIDGE_SRC.write_text(script, encoding="utf-8")
    cp = subprocess.run(["/usr/bin/osacompile", "-o", str(AS_BRIDGE_SCPT), str(AS_BRIDGE_SRC)],
                        capture_output=True, text=True, timeout=120)
    if cp.returncode != 0:
        raise OSError(f"编译 AppleScript 失败：{(cp.stderr or '').strip()}")

    raw = _run_picker("runscript", str(AS_BRIDGE_SCPT), timeout)
    if raw.startswith("OK||"):
        return raw[4:].strip()
    detail = raw.split("||", 1)[-1] if "||" in raw else raw
    raise OSError(detail or "通过 applet 执行 AppleScript 失败")


def osascript(script: str, timeout: int = 120) -> str:
    """执行 AppleScript 源码，返回 stdout（已 strip）。

    先走系统 osascript；命中 TCC -10004 时自动改由 applet 发包重试。
    """
    cp = run(["/usr/bin/osascript", "-e", script], timeout=timeout)
    if cp.returncode == 0:
        return cp.stdout.strip()
    msg = (cp.stderr or cp.stdout or "osascript 失败").strip()
    if "-10004" in msg or "权限违例" in msg or "not allowed" in msg:
        return osascript_via_picker(script, timeout)
    raise OSError(msg)


def osascript_file(path: Path, args: list[str] | None = None, timeout: int = 180) -> str:
    """执行 .applescript 文件，可传参。"""
    cmd = ["/usr/bin/osascript", str(path)] + list(args or [])
    cp = run(cmd, timeout=timeout)
    if cp.returncode != 0:
        raise OSError((cp.stderr or cp.stdout or "osascript 失败").strip())
    return cp.stdout.strip()


# ---------------------------------------------------------------- 音频

AUDIO_EXTS = {".flac", ".m4a", ".mp3", ".wav", ".aiff", ".aif", ".alac", ".ogg", ".ape", ".wv"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".flv", ".mov", ".m4v", ".ts"}


def audio_kind(path: Path) -> str:
    """判定音频真实格式，比扩展名可靠（B 站下载的常是伪装成 mp4 的音频）。"""
    ext = path.suffix.lower()
    if ext == ".flac":
        return "flac"
    if ext in {".m4a", ".alac", ".mp4", ".m4v", ".aac"}:
        return "mp4"
    if ext == ".mp3":
        return "mp3"
    if ext in {".wav", ".aiff", ".aif"}:
        return "pcm"
    return ext.lstrip(".") or "unknown"


# ---------------------------------------------------------------- Downie 偏好
# Downie 是沙盒应用，下载目录存的是「用户亲自选过」的安全书签（CMDestinationSelectionFolderURL）。
# 外部进程写进去的普通书签拿不到写权限，它只会退回 ~/Downloads，
# 所以这个目录只能由用户在 Downie 界面里选一次。

DOWNIE_DOMAIN = "com.charliemonroe.Downie-4"
# Downie 默认把文件写进容器里的 Downloads，它是一个指向真实 ~/Downloads 的软链接。
# 把这个软链接改指向临时目录，就能让 Downie「以为在下载到 Downloads」而实际落在工作目录
# （实测可行，比伪造安全书签可靠——沙盒不认外部写进去的书签）。
DOWNIE_CONTAINER_DOWNLOADS = (Path.home() / "Library/Containers"
                              / "com.charliemonroe.Downie-4/Data/Downloads")


def downie_pref(key: str) -> str:
    """读 Downie 的一个偏好值，不存在时返回空串。"""
    cp = run(["/usr/bin/defaults", "read", DOWNIE_DOMAIN, key], timeout=20)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def downie_set(key: str, *value: str) -> None:
    """写 Downie 偏好（value 需带类型参数，如 ("-bool", "true")）。"""
    run(["/usr/bin/defaults", "write", DOWNIE_DOMAIN, key, *value], timeout=20)


def _read_bookmark_path() -> str | None:
    """解析 CMDestinationSelectionFolderURL 这个安全书签指向的路径。

    书签是二进制 NSData，交给 NSURL 自己解析，不猜格式。
    """
    import base64
    import plistlib
    import tempfile

    plist = (Path.home() / "Library/Containers/com.charliemonroe.Downie-4/Data"
             "/Library/Preferences/com.charliemonroe.Downie-4.plist")
    try:
        data = plistlib.loads(plist.read_bytes()).get("CMDestinationSelectionFolderURL")
    except Exception:
        return None
    if not isinstance(data, bytes):
        return None

    script = (
        'ObjC.import("Foundation");'
        'function run(argv){'
        ' var d=$.NSData.alloc.initWithBase64EncodedStringOptions(argv[0],0);'
        ' var stale=$(), err=$();'
        ' var u=$.NSURL.URLByResolvingBookmarkDataOptionsRelativeToURLBookmarkDataIsStaleError('
        '   d,0,$(),stale,err);'
        ' return u.isNil() ? "" : ObjC.unwrap(u.path);'
        '}'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        js = fh.name
    try:
        cp = run(["/usr/bin/osascript", "-l", "JavaScript", js,
                  base64.b64encode(data).decode()], timeout=30)
        out = cp.stdout.strip()
        return out or None
    finally:
        Path(js).unlink(missing_ok=True)


def downie_destination() -> str | None:
    """返回 Downie 实际的下载落盘目录。

    自定义目录（安全书签）优先——这是 Downie 自己的设置项；
    其次是容器 Downloads 软链接（旧的重定向手段，作为兜底）；
    两者都没有时返回 None，表示默认的 ~/Downloads。
    """
    bm = _read_bookmark_path()
    if bm:
        return bm

    link = DOWNIE_CONTAINER_DOWNLOADS
    if link.is_symlink():
        target = Path(os.path.realpath(link))
        if target.resolve() != (Path.home() / "Downloads").resolve():
            return str(target)

    return None


def downie_dest_source() -> str:
    """下载目录的来源：bookmark（正路）/ symlink（旧兜底）/ default。"""
    if _read_bookmark_path():
        return "bookmark"
    link = DOWNIE_CONTAINER_DOWNLOADS
    if link.is_symlink():
        if Path(os.path.realpath(link)).resolve() != (Path.home() / "Downloads").resolve():
            return "symlink"
    return "default"


# ---------------------------------------------------------------- Downie 目录 UI 选择

PICKER_APP = SKILL_DIR / "bin" / "DownieDestPicker.app"
PICKER_SRC = SKILL_DIR / "bin" / "DownieDestPicker.applescript"      # 执行器壳（几乎不改）
PICKER_LOGIC_SRC = SKILL_DIR / "bin" / "picker.applescript"          # 业务逻辑（随时改）
PICKER_LOGIC_SCPT = Path("/tmp/downie_picker_logic.scpt")
PICKER_REQUEST = Path("/tmp/downie_pick_request.json")
PICKER_RESULT = Path("/tmp/downie_pick_result.json")


def _ensure_logic_scpt() -> Path:
    """把 bin/picker.applescript 编译成 .scpt，供执行器 load script。

    app 壳只负责「加载并调用 main()」，所以改 UI 逻辑只改 .applescript，
    重新编译这一步不碰 app 二进制，辅助功能授权不会失效。
    """
    if not PICKER_LOGIC_SRC.exists():
        raise OSError(f"缺少 {PICKER_LOGIC_SRC}")
    fresh = (PICKER_LOGIC_SCPT.exists()
             and PICKER_LOGIC_SCPT.stat().st_mtime >= PICKER_LOGIC_SRC.stat().st_mtime)
    if not fresh:
        tmp = PICKER_LOGIC_SCPT.with_name("downie_picker_logic.tmp.scpt")
        tmp.unlink(missing_ok=True)
        proc = subprocess.run(["/usr/bin/osacompile", "-o", str(tmp), str(PICKER_LOGIC_SRC)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise OSError("编译 picker 逻辑失败：" + (proc.stderr or proc.stdout).strip()[:400])
        tmp.replace(PICKER_LOGIC_SCPT)
    return PICKER_LOGIC_SCPT


def _run_picker(mode: str, target: str = "", timeout: int = 180) -> str:
    """驱动 DownieDestPicker.app，返回它的原始结果文本。

    为什么要绕一层 app：WorkBuddy 是 Electron 应用，它派生的 osascript 在 TCC 里被记成
    「osascript」而不是 WorkBuddy，导致授权给 WorkBuddy 后仍报 -10004；改用 do shell script
    转发又会报「osascript 不允许发送按键 (1002)」。独立 app 的 TCC 身份明确，一次授权长期有效。
    """
    if not PICKER_APP.exists():
        raise OSError(f"缺少 {PICKER_APP}；用 osacompile 重新编译（见 SKILL.md）")

    if mode == "check":
        lines = ["check"]
    else:
        scpt = _ensure_logic_scpt()
        lines = ["script", str(scpt), mode] + ([target] if target else [])

    PICKER_REQUEST.write_text("\n".join(lines), encoding="utf-8")
    PICKER_RESULT.unlink(missing_ok=True)

    subprocess.run(["/usr/bin/open", "-g", "-a", str(PICKER_APP)], capture_output=True, timeout=60)

    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        time.sleep(0.4)
        if not PICKER_RESULT.exists():
            continue
        try:
            text = PICKER_RESULT.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text and text == last:      # 内容稳定即认为写完
            return text
        last = text
    return last or "ERROR||等待 DownieDestPicker 超时（可能被系统弹窗挡住）"


def _parse_picker(raw: str) -> dict:
    """把 app 的结果文本转成统一结构。"""
    if raw.startswith("OK||"):
        parts = raw.split("||")
        return {"ok": True, "needs_accessibility": False,
                "detail": "点击了「{}」，确认：{}".format(
                    parts[1] if len(parts) > 1 else "?", parts[2] if len(parts) > 2 else "?")}
    if raw.startswith("NEED_PERMISSION||"):
        return {
            "ok": False, "needs_accessibility": True,
            "detail": "需要把 DownieDestPicker 加入「辅助功能」："
                      "系统设置 > 隐私与安全性 > 辅助功能 → 点 + 或直接把这个 app 拖进列表 → "
                      f"勾选。app 位置：{PICKER_APP}\n原因：{raw.split('||', 1)[1]}",
        }
    if raw.startswith("ERROR||"):
        parts = raw.split("||")
        return {"ok": False, "needs_accessibility": False,
                "detail": parts[1] if len(parts) > 1 else "未知错误",
                "debug": parts[2] if len(parts) > 2 else ""}
    return {"ok": False, "needs_accessibility": False,
            "detail": f"DownieDestPicker 无有效输出：{raw[:300]}"}


def downie_pick_folder_ui(target: Path, timeout: int = 180) -> dict:
    """用 UI 自动化在 Downie 设置里选下载目录，生成沙盒认可的安全书签。

    Downie 是沙盒应用，只有「用户在文件选择器里亲手选过」的目录才有写权限，
    外部伪造的书签会静默退回 ~/Downloads（已实测过两种写法都不行）。所以只能模拟真实点击。

    返回 {"ok": bool, "detail": str, "needs_accessibility": bool, "debug"?: str}
    """
    target = Path(target).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    try:
        raw = _run_picker("pick", str(target), timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "needs_accessibility": False, "detail": f"启动选择器失败：{exc}"}
    return _parse_picker(raw)


def downie_picker_ready(timeout: int = 90) -> dict:
    """检查 DownieDestPicker 是否已有辅助功能权限（轻量，无副作用）。"""
    try:
        raw = _run_picker("check", "", timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "needs_accessibility": False,
                "detail": f"启动选择器失败：{exc}"}
    return _parse_picker(raw)


def downie_dump_ui(timeout: int = 180, open_settings: bool = False,
                   mode: str = "dump") -> dict:
    """只读 dump Downie 的菜单栏与全部窗口 UI 树，用于校准选择器。

    open_settings=True 时先通过菜单栏把设置窗口打开再 dump；
    mode="dumpdest" 会额外切到「下载目录」分页。
    返回 {"ok":True, "windows":[{"index","name"}], "menu":[{"index","name","items":[...]}],
          "tree":[{"win","depth","role","name","value","desc"}]}
    """
    try:
        raw = _run_picker(mode, "open" if open_settings else "", timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "needs_accessibility": False, "detail": f"启动选择器失败：{exc}"}

    if raw.startswith("NEED_PERMISSION||"):
        return _parse_picker(raw)
    if not raw.startswith("OK||"):
        return {"ok": False, "needs_accessibility": False, "detail": raw[:400]}

    windows, menu, tree = [], {}, []
    sel = ""
    clk = ""
    for line in raw.split("||", 1)[1].splitlines():
        parts = line.split("|")
        tag = parts[0] if parts else ""
        if tag == "W" and len(parts) >= 3:
            windows.append({"index": int(parts[1]), "name": parts[2]})
        elif tag == "SEL" and len(parts) >= 2:
            sel = "|".join(parts[1:])
        elif tag == "CLK" and len(parts) >= 2:
            clk = "|".join(parts[1:])
        elif tag == "MB" and len(parts) >= 3:
            menu[parts[1]] = {"index": int(parts[1]), "name": parts[2], "items": []}
        elif tag == "MI" and len(parts) >= 4:
            if parts[1] in menu:
                menu[parts[1]]["items"].append({"name": parts[2], "shortcut": parts[3]})
        elif tag == "E" and len(parts) >= 7:
            tree.append({"win": parts[1], "depth": int(parts[2]) if parts[2].isdigit() else 0,
                         "role": parts[3], "name": parts[4], "value": parts[5],
                         "desc": parts[6]})
    return {"ok": True, "windows": windows, "sel": sel, "clk": clk,
            "menu": [menu[k] for k in sorted(menu, key=int)], "tree": tree}


def downie_probe_settings(timeout: int = 180) -> dict:
    """兼容旧接口：把 dump 结果压成 role/name/value 三元组列表。"""
    res = downie_dump_ui(timeout)
    if not res.get("ok"):
        return res
    rows = [{"role": e["role"], "name": e["name"], "value": e["value"]}
            for e in res["tree"]]
    return {"ok": True, "count": len(rows), "tree": rows,
            "windows": res["windows"], "menu": res["menu"]}


def downie_link_downloads(target: Path) -> None:
    """把 Downie 容器的 Downloads 软链接指向 target（幂等，已指向则跳过）。"""
    target = Path(target).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    link = DOWNIE_CONTAINER_DOWNLOADS
    if link.is_symlink():
        if Path(os.path.realpath(link)) == target:
            return
        link.unlink()
    elif link.exists():
        raise OSError(f"{link} 是真实目录，不是软链接，为避免丢文件不做改动")

    parent = link.parent
    rel = os.path.relpath(target, parent)
    link.symlink_to(rel)


# ---------------------------------------------------------------- 版本命名

# 同一首歌常有多个演绎，进同一个专辑时必须靠 title 后缀区分，
# 否则 Apple Music 里会出现两三条一模一样的「载梦行舟」，谁是谁分不清。
VERSION_ALIASES = {
    "剧情": "剧情版", "剧情mv": "剧情版", "mv": "剧情版",
    "翻唱": "翻唱版", "cover": "翻唱版",
    "纯歌": "纯享版", "纯歌版": "纯享版", "纯享": "纯享版", "纯音乐": "纯享版",
    "伴奏": "伴奏版", "instrumental": "伴奏版",
    "现场": "现场版", "live": "现场版",
    "翻奏": "翻奏版",
}


def normalize_version(version: str) -> str:
    """把「剧情」「cover」「live」这类简写收敛成统一后缀，保证同一个词只对应一种写法。"""
    v = (version or "").strip().strip("（）()[]【】 ")
    if not v:
        return ""
    return VERSION_ALIASES.get(v.lower(), v)


def apply_version(meta: dict) -> dict:
    """把 version 拼进 title：`载梦行舟` + `翻唱版` → `载梦行舟（翻唱版）`。

    幂等：title 里已经有这个后缀时不再重复添加。
    只改 title，不动 album —— 同一专辑靠 album_artist 归组，靠 title 区分曲目。
    """
    version = normalize_version(meta.get("version", ""))
    title = (meta.get("title") or "").strip()
    if not version or not title:
        return meta
    if version in title:
        return meta
    out = dict(meta)
    out["title"] = f"{title}（{version}）"
    return out


def versioned_filename(title: str, ext: str) -> str:
    """按版本命名约定生成文件名，与 title 保持一致。"""
    return f"{(title or '').strip()}{ext if ext.startswith('.') else '.' + ext}"


def downie_restore_downloads() -> None:
    """把容器 Downloads 还原成指向真实的 ~/Downloads。"""
    link = DOWNIE_CONTAINER_DOWNLOADS
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        raise OSError(f"{link} 是真实目录，不做改动")
    link.symlink_to("../../../../Downloads")
