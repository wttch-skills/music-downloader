#!/usr/bin/env python3
"""UI 结构探测：把某个应用的辅助功能（Accessibility）树 dump 出来。

Music Converter 没有 AppleScript 字典，只能靠 UI 自动化操控，而 UI 自动化
依赖准确的元素层级。界面随版本变化时，用这个脚本重新抓一遍结构即可。

用法：
  ui_probe.py --app "Music Converter" [--depth 6] [--max 400]
"""
from __future__ import annotations

import argparse
import json

from _common import emit, fail, osascript

# AppleScript 递归 dump：返回 角色|标题|描述|位置 的扁平列表
SCRIPT_TMPL = '''
on walk(el, depth, maxDepth, acc)
  if depth > maxDepth then return acc
  try
    set role to (role of el) as text
  on error
    set role to "?"
  end try
  set nm to ""
  set desc to ""
  try
    set nm to (name of el) as text
  on error
    try
      set nm to (value of el) as text
    on error
      set nm to ""
    end try
  end try
  try
    set desc to (description of el) as text
  on error
    set desc to ""
  end try
  set end of acc to (depth as text) & "|" & role & "|" & nm & "|" & desc
  try
    set kids to UI elements of el
  on error
    set kids to {}
  end try
  repeat with k in kids
    set acc to my walk(k, depth + 1, maxDepth, acc)
  end repeat
  return acc
end walk

tell application "System Events"
  tell process "%(proc)s"
    set frontmost to true
    set out to my walk(window 1, 0, %(depth)d, {})
    set AppleScript's text item delimiters to linefeed
    set txt to out as text
    set AppleScript's text item delimiters to ""
    return txt
  end tell
end tell
'''


def main() -> None:
    ap = argparse.ArgumentParser(description="dump 应用 UI 树")
    ap.add_argument("--app", required=True, help="进程名，如 'Music Converter'")
    ap.add_argument("--depth", type=int, default=6)
    args = ap.parse_args()

    script = SCRIPT_TMPL % {"proc": args.app, "depth": args.depth}
    try:
        out = osascript(script, timeout=90)
    except OSError as exc:
        msg = str(exc)
        hint = ""
        if "-10004" in msg or "权限违例" in msg or "not allowed" in msg.lower():
            hint = ("需要授权：系统设置 > 隐私与安全性 > 辅助功能，勾选运行本 skill 的程序"
                    "（WorkBuddy / 终端）。授权后需重启该程序。")
        elif "-1728" in msg:
            hint = f"找不到进程「{args.app}」，请先启动该应用。"
        fail(f"读取 UI 失败：{msg}", hint=hint)

    rows = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 3 and parts[0].isdigit():
            rows.append({
                "depth": int(parts[0]),
                "role": parts[1],
                "name": parts[2],
                "desc": parts[3] if len(parts) > 3 else "",
            })
    emit({"ok": True, "app": args.app, "count": len(rows), "tree": rows})


if __name__ == "__main__":
    main()
