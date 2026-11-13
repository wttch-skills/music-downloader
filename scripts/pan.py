#!/usr/bin/env python3
"""网盘分享链接的浏览器自动化（百度网盘 / 夸克 / 阿里云盘）。

思路：在用户已登录的 Chrome 里打开分享页 → 自动填提取码 → 读出文件列表 →
转存到用户自己的网盘 → 用客户端下载，脚本监控下载目录回收文件。

为什么不直接下：网盘对这类文件都要客户端，网页直链需要 sign+timestamp 且非会员限速到不可用。
转存 + 客户端下载是唯一稳的路子。

用法：
  pan.py open  --url "https://pan.baidu.com/s/xxx" --code abcd [--save] [--slug 歌名]
  pan.py wait  --slug 歌名 [--timeout 1800]
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import cdp
from _common import AUDIO_EXTS, emit, fail, log, session_dir

# 百度网盘客户端默认下载目录
BAIDU_DL_DIRS = [
    Path.home() / "Downloads" / "BaiduNetdiskDownload",
    Path.home() / "Downloads" / "百度网盘下载",
    Path.home() / "Downloads",
    Path("/Volumes/data/BaiduNetdiskDownload"),
]

# 判定当前页面状态用的关键文本
STATE_HINTS = {
    "code_input": ["提取码", "请输入提取码", "输入提取码"],
    "need_login": ["请登录", "登录后", "扫码登录"],
    "err_code": ["提取码错误", "密码错误", "提取码不正确"],
    "err_gone": ["链接不存在", "已失效", "分享已取消", "来晚了"],
    "file_list": ["保存到网盘", "下载", "文件名", "大小"],
}

OPEN_JS = r"""
(() => {
  const t = document.body ? document.body.innerText : '';
  const inputs = [...document.querySelectorAll('input')].map(i => ({
    type: i.type, placeholder: i.getAttribute('placeholder') || '',
    id: i.id || '', cls: (i.className || '').slice(0, 60),
    visible: !!(i.offsetWidth || i.offsetHeight)
  })).filter(i => i.visible);
  return JSON.stringify({
    url: location.href, title: document.title,
    text: t.slice(0, 4000), textLen: t.length,
    inputs: inputs.slice(0, 20),
    hasIframe: !!document.querySelector('iframe')
  });
})()
"""

FILL_CODE_JS_TMPL = r"""
(() => {
  const code = %s;
  const cands = [...document.querySelectorAll('input')].filter(i =>
    (i.offsetWidth || i.offsetHeight) && i.type !== 'hidden');
  if (!cands.length) return JSON.stringify({ok: false, reason: 'no_input'});
  // 优先 placeholder/id/class 里带「提取码」的输入框
  let target = cands.find(i =>
    /提取码|密码|pwd|code/i.test((i.getAttribute('placeholder') || '') + i.id + (i.className || '')));
  if (!target) target = cands[cands.length - 1];

  target.focus();
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(target, code);
  target.dispatchEvent(new Event('input', {bubbles: true}));
  target.dispatchEvent(new Event('change', {bubbles: true}));

  // 点确定/提取
  const texts = ['提取文件', '确定', '提交', '提取', '确认'];
  const clickables = [...document.querySelectorAll('a, button, span, div')]
    .filter(e => (e.offsetWidth || e.offsetHeight) && e.children.length === 0);
  for (const t of texts) {
    const el = clickables.find(e => (e.innerText || '').trim() === t);
    if (el) { el.click(); return JSON.stringify({ok: true, clicked: t}); }
  }
  // 没找到按钮就回车提交
  target.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', keyCode: 13, bubbles: true}));
  return JSON.stringify({ok: true, clicked: 'enter'});
})()
"""

LIST_FILES_JS = r"""
(() => {
  const t = document.body ? document.body.innerText : '';
  // 文件名一般以音频/压缩包扩展名结尾
  const names = new Set();
  const re = /([^\s/\\:*?"<>|]{1,120}\.(?:flac|mp3|m4a|wav|ape|wv|zip|rar|7z|iso|dsf|dff))/gi;
  let m;
  while ((m = re.exec(t)) !== null) names.add(m[1].trim());

  // DOM 里的文件名节点（百度网盘新版用 class 含 file-name / filename）
  document.querySelectorAll('[class*="file-name"], [class*="filename"], [class*="FileItem"], .name').forEach(e => {
    const s = (e.innerText || '').trim();
    if (s && s.length < 160) names.add(s);
  });

  return JSON.stringify({
    url: location.href, title: document.title,
    text: t.slice(0, 3000),
    files: [...names].slice(0, 40),
    hasSaveBtn: /保存到网盘|保存全部/.test(t),
    hasDownloadBtn: /下载/.test(t)
  });
})()
"""

SAVE_TO_PAN_JS_TMPL = r"""
(() => {
  // 勾选全部文件，再点「保存到网盘」
  const boxes = [...document.querySelectorAll('input[type=checkbox]')]
    .filter(b => (b.offsetWidth || b.offsetHeight));
  boxes.forEach(b => { if (!b.checked) b.click(); });

  const all = [...document.querySelectorAll('a, button, span, div')]
    .filter(e => (e.offsetWidth || e.offsetHeight));
  const saveBtn = all.find(e => /保存到网盘|保存全部|转存/.test((e.innerText || '').trim()))
               || all.find(e => /保存/.test((e.innerText || '').trim()) && (e.innerText || '').length < 10);
  if (!saveBtn) return JSON.stringify({ok: false, reason: 'no_save_button', checked: boxes.length});
  saveBtn.click();
  return JSON.stringify({ok: true, clicked: (saveBtn.innerText || '').trim(), checked: boxes.length});
})()
"""


def detect_kind(url: str) -> str:
    if "pan.baidu.com" in url:
        return "baidu"
    if "pan.quark.cn" in url or "quark.cn" in url:
        return "quark"
    if "aliyundrive.com" in url or "alipan.com" in url:
        return "aliyun"
    return "unknown"


def has_code_input(info: dict) -> bool:
    """页面里有没有「提取码」输入框。

    不能只靠 innerText 判断：百度/夸克的提取码页，提示语是 input 的 placeholder，
    不进 innerText，所以正文里根本看不到「提取码」三个字（实测百度 init 页就是如此）。
    """
    for i in info.get("inputs", []):
        blob = f"{i.get('placeholder', '')}{i.get('id', '')}{i.get('cls', '')}".lower()
        if "提取码" in blob or "accesscode" in blob or "pwd" in blob or "passcode" in blob:
            return True
    return False


def page_state(info: dict) -> str:
    text = info.get("text", "")
    # 硬错误优先
    for state in ("err_gone", "err_code"):
        if any(h in text for h in STATE_HINTS[state]):
            return state
    # 文件列表优先于登录：分享页常常「能看到文件列表 + 角落挂个登录入口」，
    # 先判 need_login 会把可用的分享误判成要登录（实测夸克/百度都会这样）
    if any(h in text for h in STATE_HINTS["file_list"]):
        return "file_list"
    if has_code_input(info):
        return "code_input"
    for state in ("need_login", "code_input"):
        if any(h in text for h in STATE_HINTS[state]):
            return state
    return "loading" if info.get("textLen", 0) < 50 else "unknown"


# ---------------------------------------------------------------- open

def cmd_open(args) -> None:
    kind = detect_kind(args.url)
    if kind == "unknown":
        fail(f"不认识的网盘类型：{args.url}")

    tid = cdp.new_tab(args.url)
    try:
        cdp.wait_ready(tid, timeout=45)
        time.sleep(3)

        info = json.loads(cdp.eval_js(tid, OPEN_JS))
        state = page_state(info)
        log(f"页面状态：{state}（{info.get('title')}）")

        # 分享页可能在 iframe 里（百度旧版）。但百度现在的 init 页也会挂一个广告 iframe，
        # 那不是正文——所以页面已有提取码输入框时不要进 iframe，否则会读到广告页。
        if (state not in {"code_input", "file_list"} and info.get("hasIframe")
                and not has_code_input(info)):
            log("  页面含 iframe，尝试进入内层文档")
            cdp.eval_js(tid, "(() => {const f=document.querySelector('iframe');"
                             "if(f && f.contentDocument) return 'ok'; return 'blocked'})()")
            time.sleep(2)
            info = json.loads(cdp.eval_js(tid, OPEN_JS))
            state = page_state(info)
            log(f"  内层状态：{state}")

        if state == "err_gone":
            fail("分享链接已失效", url=args.url)
        if state == "need_login":
            fail(f"{kind} 需要登录。请在 Chrome 里登录该网盘后重试。", url=args.url)

        if state == "code_input":
            if not args.code:
                fail("该分享需要提取码，但没提供 --code", url=args.url)
            log(f"填入提取码 {args.code}")
            res = json.loads(cdp.eval_js(tid, FILL_CODE_JS_TMPL % json.dumps(args.code)))
            log(f"  填码结果：{res}")
            if not res.get("ok"):
                fail("提取码输入失败，页面结构可能已变化", detail=res, url=args.url)
            time.sleep(4)
            cdp.wait_ready(tid, timeout=25)
            time.sleep(1.5)
            info = json.loads(cdp.eval_js(tid, OPEN_JS))
            state = page_state(info)
            log(f"提交后状态：{state}")

        if state == "err_code":
            fail("提取码错误", url=args.url, code=args.code)

        listing = json.loads(cdp.eval_js(tid, LIST_FILES_JS))
        files = listing.get("files", [])
        log(f"识别到 {len(files)} 个文件")

        saved = False
        if args.save and listing.get("hasSaveBtn"):
            log("尝试转存到我的网盘…")
            res = json.loads(cdp.eval_js(tid, SAVE_TO_PAN_JS_TMPL))
            log(f"  转存结果：{res}")
            time.sleep(3)
            # 转存后常弹「选择保存位置」窗口，默认目录直接确认
            confirm = cdp.eval_js(tid, r"""
              (() => {
                const all = [...document.querySelectorAll('a, button, span')]
                  .filter(e => (e.offsetWidth || e.offsetHeight));
                const ok = all.find(e => /^(确定|确认|保存)$/.test((e.innerText || '').trim()));
                if (ok) { ok.click(); return 'confirmed'; }
                return 'no_dialog';
              })()
            """)
            log(f"  确认对话框：{confirm}")
            saved = str(confirm) == "confirmed"

        audio = [f for f in files if Path(f).suffix.lower() in AUDIO_EXTS
                 or Path(f).suffix.lower() in {".zip", ".rar", ".7z", ".dsf", ".dff"}]

        emit({
            "ok": True,
            "kind": kind,
            "url": listing.get("url", args.url),
            "state": state,
            "saved_to_pan": saved,
            "files": files,
            "audio_files": audio,
            "has_download_button": listing.get("hasDownloadBtn"),
            "next_hint": (
                "已转存到你的网盘。请在百度网盘客户端里找到这些文件并下载，"
                "或运行 pan.py wait 监控下载目录。"
            ) if saved else "未转存。可在分享页手动点「保存到网盘」，或加 --save 重试。",
        })
    finally:
        cdp.close_tab(tid)


# ---------------------------------------------------------------- wait

def cmd_wait(args) -> None:
    """监控网盘客户端下载目录，等文件下载完再挪进任务目录。"""
    workdir = session_dir(args.slug)
    dirs = [d for d in BAIDU_DL_DIRS if d.exists()]
    if not dirs:
        fail("找不到网盘下载目录", looked=[str(d) for d in BAIDU_DL_DIRS])

    log(f"监控目录：{', '.join(str(d) for d in dirs)}")
    deadline = time.time() + args.timeout
    seen: dict[str, tuple[int, float]] = {}

    while time.time() < deadline:
        time.sleep(4)
        found = []
        for d in dirs:
            try:
                for p in d.rglob("*"):
                    if not p.is_file():
                        continue
                    if p.suffix.lower() not in AUDIO_EXTS | {".zip", ".rar", ".7z", ".dsf", ".dff"}:
                        continue
                    if ".baiduyun" in p.name or p.name.endswith(".tmp") or p.name.endswith(".dwl"):
                        continue
                    st = p.stat()
                    found.append((p, st.st_size))
            except OSError:
                continue

        for p, size in found:
            key = str(p)
            prev = seen.get(key)
            seen[key] = (size, time.time())
            # 体积稳定 10 秒视为下载完成（网盘客户端先写 .dwl 临时名）
            if prev and prev[0] == size and size > 10240 and time.time() - prev[1] >= 10:
                target = workdir / p.name
                try:
                    if p.resolve() != target.resolve():
                        p.replace(target)
                    emit({"ok": True, "file": str(target), "size": size})
                    return
                except OSError as exc:
                    log(f"  移动失败：{exc}")

        active = len([k for k, (s, t) in seen.items() if time.time() - t < 15])
        if active:
            log(f"  {active} 个文件正在下载…")

    fail("等待超时，没发现下载完成的文件",
         hint="确认百度网盘客户端已启动且正在下载；也可手动把文件放到任务目录后继续。")


def main() -> None:
    ap = argparse.ArgumentParser(description="网盘分享链接自动化")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_o = sub.add_parser("open", help="打开分享页、填码、列文件、可选转存")
    p_o.add_argument("--url", required=True)
    p_o.add_argument("--code", default="")
    p_o.add_argument("--save", action="store_true", help="转存到自己的网盘")
    p_o.add_argument("--slug", default="")

    p_w = sub.add_parser("wait", help="监控网盘下载目录")
    p_w.add_argument("--slug", required=True)
    p_w.add_argument("--timeout", type=int, default=1800)

    args = ap.parse_args()
    {"open": cmd_open, "wait": cmd_wait}[args.cmd](args)


if __name__ == "__main__":
    main()
