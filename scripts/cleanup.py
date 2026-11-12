#!/usr/bin/env python3
"""收尾清理：临时文件 + Downie 历史记录。

这个 skill 跑完一条链路后不应该留下任何痕迹：
  · 下载产物、转码中间文件、封面图都堆在 <工作目录>/tmp/music/<任务名>/
  · Downie 自己的「已完成下载」列表会把每条历史都留着

用法：
  cleanup.py                      # 清所有任务目录 + 清空 Downie 历史（默认）
  cleanup.py --slug 晴天           # 只清某个任务目录
  cleanup.py --history            # 只清 Downie 历史
  cleanup.py --keep-audio         # 清中间文件但保留音频成品
  cleanup.py --dry-run            # 只列出将删除什么，不动手

输出：{"ok":true,"removed":[...],"kept":[...],"history":"...","freed_bytes":N}
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from _common import AUDIO_EXTS, TMP_ROOT, emit, fail, log

# 清历史时用到的 UI 动作（见 bin/picker.applescript）
PICKER_ACTIONS = {"clearhistory": "清空 Downie 的已完成/未激活列表"}


def _safe_targets(slug: str) -> list[Path]:
    """要清理的目录，永远只在 TMP_ROOT 里面。"""
    root = TMP_ROOT.resolve()
    if slug:
        cand = (root / slug).resolve()
        if root not in cand.parents and cand != root:
            fail(f"拒绝清理 {cand}：不在临时目录 {root} 内")
        return [cand] if cand.exists() else []
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())


def _audio_files(path: Path) -> list[Path]:
    return [p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS]


def _downie_busy() -> bool:
    """Downie 还有活任务时，别动它的 .DownieTemporaryFolder，否则会把下载搞坏。"""
    try:
        from downie import queue_active
        return queue_active()
    except Exception:                                            # noqa: BLE001
        return False


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _stray_files() -> list[Path]:
    """直接躺在 tmp/music 根目录的散落产物（早期落到根目录、或手滑留下的）。"""
    root = TMP_ROOT.resolve()
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_file() and not p.is_symlink())


def clean_files(slug: str, keep_audio: bool, dry_run: bool) -> dict:
    removed, kept = [], []
    freed = 0
    busy = _downie_busy()

    # 根目录里的散落文件（只在全量清理时处理）
    if not slug:
        for f in _stray_files():
            if keep_audio and f.suffix.lower() in AUDIO_EXTS:
                kept.append(str(f))
                continue
            freed += f.stat().st_size
            removed.append(str(f))
            if not dry_run:
                f.unlink(missing_ok=True)

    for target in _safe_targets(slug):
        # Downie 自己的临时区：有活动任务时跳过，否则会把进行中的下载弄坏
        if busy and (target.name == ".DownieTemporaryFolder"
                     or target.suffix == ".downiepart"):
            kept.append(f"{target}（Downie 仍有活动任务，跳过）")
            continue
        if keep_audio:
            audios = _audio_files(target)
            for f in sorted(target.rglob("*"), key=lambda p: -len(p.parts)):
                if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                    kept.append(str(f))
                    continue
                if f.is_file():
                    freed += f.stat().st_size
                    removed.append(str(f))
                    if not dry_run:
                        f.unlink(missing_ok=True)
            # 收掉空的子目录
            if not dry_run:
                for d in sorted((p for p in target.rglob("*") if p.is_dir()),
                                key=lambda p: -len(p.parts)):
                    try:
                        d.rmdir()
                    except OSError:
                        pass
            # 只剩音频时，任务目录保留
            if audios:
                continue
        freed += _size(target)
        if dry_run:
            removed.append(str(target) + "/")
            continue
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            kept.append(f"{target}（删除失败，可能仍被占用）")
        else:
            removed.append(str(target) + "/")

    return {"removed": removed, "kept": kept, "freed_bytes": freed}


def clean_history(dry_run: bool = False) -> dict:
    """清空 Downie 的历史列表。

    优先走「文件 > 清空已完成的下载 / 清空未激活的下载」菜单，
    和用户手点完全一致；没有辅助功能权限时退回到直接清偏好里的历史数组
    （需要 Downie 退出后才会被写回，所以那一步要重启它）。
    """
    if dry_run:
        return {"ok": True, "method": "dry-run", "detail": "将清空 Downie 历史"}

    try:
        from _common import _ensure_logic_scpt, _parse_picker, _run_picker
        _ensure_logic_scpt()
        res = _parse_picker(_run_picker("clearhistory", "", 150))
        if res.get("ok"):
            return {"ok": True, "method": "ui-menu", "detail": res["detail"]}
        ui_detail = res.get("detail", "")
    except (OSError, subprocess.SubprocessError) as exc:
        ui_detail = f"UI 方式不可用：{exc}"

    # 降级：直接改偏好里的历史数组
    try:
        from _common import downie_pref
        proc = subprocess.run(
            ["/usr/bin/defaults", "write", "com.charliemonroe.Downie-4",
             "XUSavedDownloads", "-array"],
            capture_output=True, text=True)
        if proc.returncode != 0:
            return {"ok": False, "detail": f"UI 失败（{ui_detail}），写偏好也失败：{proc.stderr.strip()[:200]}"}
        return {"ok": True, "method": "prefs",
                "detail": f"已清空 XUSavedDownloads 数组（UI 方式失败：{ui_detail}）；"
                          "重启 Downie 后列表才会变空"}
    except Exception as exc:                                    # noqa: BLE001
        return {"ok": False, "detail": f"清理历史失败：{exc}；UI 侧：{ui_detail}"}


def main() -> None:
    ap = argparse.ArgumentParser(description="清理临时文件与 Downie 历史")
    ap.add_argument("--slug", default="", help="只清这个任务目录")
    ap.add_argument("--all", action="store_true", help="清所有任务目录（默认行为）")
    ap.add_argument("--history", action="store_true", help="只清 Downie 历史")
    ap.add_argument("--keep-audio", action="store_true", help="保留音频成品，只删中间文件")
    ap.add_argument("--dry-run", action="store_true", help="只列出会删什么")
    args = ap.parse_args()

    do_files = not args.history
    do_history = args.history or args.all or not args.slug

    path = str(TMP_ROOT)

    files = {"removed": [], "kept": [], "freed_bytes": 0}
    if do_files:
        files = clean_files(args.slug, args.keep_audio, args.dry_run)
        for r in files["removed"]:
            log(f"  删除 {r}")
        for k in files["kept"]:
            log(f"  保留 {k}")

    hist = {"ok": True, "detail": "未执行"}
    if do_history:
        hist = clean_history(args.dry_run)
        log(f"  Downie 历史：{hist.get('detail', '')}")

    # UI 选择器的请求/结果文件是纯中间物，顺手清掉（编译缓存留着，下次省一次 osacompile）
    for tmpf in ("/tmp/downie_pick_request.json", "/tmp/downie_pick_result.json"):
        if not args.dry_run:
            Path(tmpf).unlink(missing_ok=True)

    emit({
        "ok": hist.get("ok", True),
        "tmp_root": path,
        "removed": files["removed"],
        "kept": files["kept"],
        "freed_mb": round(files["freed_bytes"] / 1048576, 2),
        "history": hist.get("detail", ""),
    })


if __name__ == "__main__":
    main()
