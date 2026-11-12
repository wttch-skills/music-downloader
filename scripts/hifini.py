#!/usr/bin/env python3
"""hifini.com.cn（音乐磁场）搜索与取链。

站点是 Discuz 系论坛，且有 JS 反爬，只能通过真实浏览器操作。
流程：
  search   关键词 -> 候选列表（供用户挑选）
  resolve  打开帖子 -> 若内容被隐藏则自动回复 -> 提取网盘/直链地址与提取码

复用用户 Chrome 的登录态，所以「回复」这一步用的就是用户自己的账号，
回复内容默认「谢谢分享」，可用 --text 覆盖。

用法：
  hifini.py search --query "晴天 周杰伦"
  hifini.py resolve --url <帖子地址> [--text "谢谢分享"]
"""
from __future__ import annotations

import argparse
import json
import re
import time

import cdp
from _common import emit, fail, log, session_dir

SITE = "https://www.hifini.com.cn"

# 网盘与直链的识别规则
PAN_PATTERNS = [
    ("baidu", r"https?://pan\.baidu\.com/s/[\w\-~]+"),
    ("quark", r"https?://pan\.quark\.cn/s/[\w\-]+"),
    ("aliyun", r"https?://(?:www\.)?aliyundrive\.com/s/[\w\-]+"),
    ("aliyundrive", r"https?://(?:www\.)?alipan\.com/s/[\w\-]+"),
    ("tianyi", r"https?://cloud\.189\.cn/t/[\w\-]+"),
    ("115", r"https?://(?:115|anxia)\.com/s/[\w\-]+"),
    ("lanzou", r"https?://[\w\-]+\.lanzou[a-z]?\.com/[\w\-/]+"),
    ("weiyun", r"https?://share\.weiyun\.com/[\w\-]+"),
    ("123pan", r"https?://(?:www\.)?123pan\.com/s/[\w\-]+"),
]
CODE_PATTERN = r"(?:提取码|密码|访问码|提取碼|pwd|code)[\s:：=]*([A-Za-z0-9]{4,8})"

# 隐藏内容提示的常见措辞
HIDDEN_HINTS = [
    "隐藏内容", "回复后查看", "回复可见", "回复后显示",
    "登录后查看", "需要回复", "请先回复",
]

THREAD_HREF = re.compile(r"(thread-\d+|/thread/|forum\.php\?mod=viewthread|/t/\d+)")

# 站点搜索表单实测为 action="?search.htm" + input name=keyword（GET）
SEARCH_URL_TMPL = SITE + "/?search.htm&keyword={}"

# 公告、版规、求曲帖，不属于音源
ADMIN_HINTS = ["[小火]", "[必火]", "[火火]", "版规", "说明", "求曲", "求歌",
               "发贴必看", "求一首", "悬赏", "积分", "VIP"]

# 标题里直接标了音质/格式，是选源的关键信息
FORMAT_HINTS = ["臻品母带", "母带", "Hi-Res", "HIRES", "24Bit", "24bit", "192Khz",
                "96Khz", "DSD", "FLAC", "WAV", "APE", "MP3-320", "320K", "320k",
                "内嵌封面", "内嵌歌词", "原版", "超清"]

SEARCH_ENTRY_JS = r"""
(() => {
  // 找出页面上的搜索表单：返回 action、输入框名、method
  const forms = [...document.querySelectorAll('form')];
  for (const f of forms) {
    const inputs = [...f.querySelectorAll('input[type=text], input[type=search], input:not([type])')];
    for (const i of inputs) {
      const n = (i.getAttribute('name') || '').toLowerCase();
      const ph = (i.getAttribute('placeholder') || '');
      if (/kw|key|word|q$|search|wd/.test(n) || /搜索|search/i.test(ph)) {
        return JSON.stringify({
          action: f.getAttribute('action') || location.href,
          method: (f.getAttribute('method') || 'get').toLowerCase(),
          param: i.getAttribute('name'),
          found: true
        });
      }
    }
  }
  return JSON.stringify({found: false});
})()
"""

SEARCH_RESULTS_JS = r"""
(() => {
  const abs = u => { try { return new URL(u, location.href).href } catch (e) { return u } };
  const out = [];
  const seen = new Set();
  document.querySelectorAll('a[href]').forEach(a => {
    const href = abs(a.getAttribute('href'));
    if (!/thread-\d+|forum\.php\?mod=viewthread|\/t\/\d+/.test(href)) return;
    if (seen.has(href)) return;
    seen.add(href);
    // 取最近的容器文本，用来判断歌手/专辑/格式
    let ctx = '';
    let el = a.parentElement;
    for (let i = 0; i < 4 && el; i++) {
      ctx = (el.innerText || '').trim();
      if (ctx.length > 8) break;
      el = el.parentElement;
    }
    out.push({
      href,
      text: (a.textContent || '').trim().replace(/\s+/g, ' '),
      context: ctx.replace(/\s+/g, ' ').slice(0, 200)
    });
  });
  return JSON.stringify({url: location.href, title: document.title, count: out.length, results: out});
})()
"""

POST_INFO_JS = r"""
(() => {
  const abs = u => { try { return new URL(u, location.href).href } catch (e) { return u } };
  const body = document.body ? document.body.innerText : '';
  const links = [];
  document.querySelectorAll('a[href]').forEach(a => {
    const href = abs(a.getAttribute('href'));
    const text = (a.textContent || '').trim();
    links.push({href, text: text.slice(0, 120)});
  });
  // 回复框
  const tas = [...document.querySelectorAll('textarea')].map(t => ({
    id: t.id || '', name: t.getAttribute('name') || '',
    placeholder: t.getAttribute('placeholder') || '',
    visible: !!(t.offsetWidth || t.offsetHeight)
  }));
  // 提交按钮
  const btns = [...document.querySelectorAll('button, input[type=submit], a.btn, .btn')].map(b => ({
    tag: b.tagName.toLowerCase(), id: b.id || '',
    text: (b.innerText || b.value || '').trim().slice(0, 40),
    onclick: (b.getAttribute('onclick') || '').slice(0, 120),
    visible: !!(b.offsetWidth || b.offsetHeight)
  }));
  // 是否已登录：页面上通常有「登录/注册」或用户名
  const hasLogin = /登录|注册|Login/.test(body.slice(0, 3000));
  return JSON.stringify({
    url: location.href,
    title: document.title,
    text: body.slice(0, 6000),
    textLen: body.length,
    links: links.slice(0, 400),
    textareas: tas,
    buttons: btns.filter(b => /回复|提交|发表|回帖|reply|submit/i.test(b.text) || /reply|fastpost/i.test(b.id)).slice(0, 20),
    loginHint: hasLogin
  });
})()
"""

REPLY_JS_TMPL = r"""
(() => {
  const msg = %s;
  const candidates = [...document.querySelectorAll('textarea')].filter(t => t.offsetWidth || t.offsetHeight);
  if (!candidates.length) return JSON.stringify({ok:false, reason:'no_textarea'});
  const ta = candidates.sort((a,b) => (b.offsetWidth*b.offsetHeight) - (a.offsetWidth*a.offsetHeight))[0];
  ta.focus();
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
  setter.call(ta, msg);
  ta.dispatchEvent(new Event('input', {bubbles:true}));
  ta.dispatchEvent(new Event('change', {bubbles:true}));
  // 找提交按钮：优先同一表单内
  const form = ta.closest('form');
  let btn = null;
  if (form) {
    btn = form.querySelector('button[type=submit], input[type=submit], button:not([type])');
  }
  if (!btn) {
    btn = [...document.querySelectorAll('button, input[type=submit], a')]
      .find(b => /^(发表回复|回复|提交|发布|submit|reply)$/i.test((b.innerText || b.value || '').trim()));
  }
  if (!btn) return JSON.stringify({ok:false, reason:'no_button', filled:true});
  btn.click();
  return JSON.stringify({ok:true, clicked:(btn.innerText || btn.value || '').trim(), textarea: ta.id || ta.name});
})()
"""


# ---------------------------------------------------------------- 工具

def parse_downloads(page_text: str, links: list[dict]) -> list[dict]:
    """从正文 + 链接里提取下载地址与提取码。"""
    found: dict[str, dict] = {}
    haystack = page_text + "\n" + "\n".join(
        f"{l.get('href','')} {l.get('text','')}" for l in links)

    for kind, pat in PAN_PATTERNS:
        for m in re.finditer(pat, haystack):
            url = m.group(0)
            if url in found:
                continue
            # 提取码常紧跟链接或写在附近，取链接后 200 字符内查找
            tail = haystack[m.end(): m.end() + 200]
            code = ""
            cm = re.search(CODE_PATTERN, tail) or re.search(CODE_PATTERN, haystack)
            if cm:
                code = cm.group(1)
            found[url] = {"type": kind, "url": url, "code": code,
                          "requires_client": kind != "direct"}

    # 直链音频 / 论坛附件
    for l in links:
        href = l.get("href", "")
        if re.search(r"\.(flac|mp3|m4a|wav|ape|wv|zip|rar|7z)(\?|$)", href, re.I) \
                or re.search(r"mod=attachment", href):
            if href not in found:
                found[href] = {"type": "direct", "url": href, "code": "",
                               "requires_client": False,
                               "label": l.get("text", "")[:60]}

    return list(found.values())


def is_hidden(text: str) -> bool:
    return any(h in text for h in HIDDEN_HINTS)


def is_login_page(title: str, text: str) -> bool:
    """未登录时站点会把请求重定向到登录页。"""
    if "登录" in (title or "") and len(text or "") < 3000:
        return True
    return bool(re.search(r"用户登录|请先登录|立即登录", (text or "")[:800]))


def extract_format(title: str) -> str:
    """从帖子标题里挑出音质/格式标记，供用户选源时参考。"""
    hits = [h for h in FORMAT_HINTS if h.lower() in title.lower()]
    # 去重（FLAC 与 flac）
    uniq = []
    for h in hits:
        if h.lower() not in {u.lower() for u in uniq}:
            uniq.append(h)
    return " / ".join(uniq)


def is_audio_post(title: str) -> bool:
    """排除版规、求曲、站务等非音源帖。"""
    if any(h in title for h in ADMIN_HINTS):
        return False
    return len(title) >= 4


# ---------------------------------------------------------------- 子命令

def cmd_search(args) -> None:
    tid = cdp.new_tab(SITE)
    try:
        cdp.wait_ready(tid, timeout=40)
        time.sleep(2)

        # 实测搜索入口是 action="?search.htm" + keyword。先用它，再补两个兜底形式
        urls = [SEARCH_URL_TMPL.format(cdp.encode(args.query))]
        try:
            entry = json.loads(cdp.eval_js(tid, SEARCH_ENTRY_JS))
            if entry.get("found") and entry.get("param") and entry["param"] != "keyword":
                action = entry["action"]
                if not action.startswith("http"):
                    action = SITE + ("/" if not action.startswith("/") else "") + action.lstrip("/")
                urls.insert(0, f"{action}{'&' if '?' in action else '?'}{entry['param']}={cdp.encode(args.query)}")
        except Exception:
            pass
        urls.append(f"{SITE}/search.php?mod=forum&srchtxt={cdp.encode(args.query)}&searchsubmit=yes")

        best = None
        for u in urls:
            log(f"尝试搜索：{u}")
            cdp.navigate(tid, u)
            time.sleep(2.5)
            cdp.wait_ready(tid, timeout=25)
            time.sleep(1)
            try:
                data = json.loads(cdp.eval_js(tid, SEARCH_RESULTS_JS))
            except Exception as exc:
                log(f"  解析失败：{exc}")
                continue

            if is_login_page(data.get("title", ""), ""):
                fail("hifini 需要登录才能搜索。请先在 Chrome 里登录 hifini.com.cn，再重试。",
                     url=u, redirect_to_login=True)

            results = [r for r in data.get("results", []) if r.get("text")]
            log(f"  命中 {len(results)} 个帖子链接")
            if results:
                best = {"entry": u, "data": data, "results": results}
                break

        if not best:
            fail("hifini 搜索没有拿到结果。可能是关键词太偏、触发风控，或站点搜索入口变更。",
                 tried=urls)

        items = []
        for r in best["results"]:
            title = (r["text"] or "").strip()
            if not is_audio_post(title):
                continue
            items.append({
                "title": title,
                "url": r["href"],
                "format": extract_format(title),
                "context": r.get("context", ""),
            })

        emit({"ok": True, "query": args.query, "search_url": best["entry"],
              "page_title": best["data"].get("title"), "count": len(items),
              "results": items[:25]})
    finally:
        cdp.close_tab(tid)


def cmd_resolve(args) -> None:
    tid = cdp.new_tab(args.url)
    try:
        cdp.wait_ready(tid, timeout=45)
        time.sleep(2)
        info = json.loads(cdp.eval_js(tid, POST_INFO_JS))

        if is_login_page(info.get("title", ""), info.get("text", "")):
            fail("打开帖子后被重定向到登录页，说明 Chrome 里 hifini 的登录态已失效。"
                 "请在 Chrome 中重新登录 hifini.com.cn 后重试。",
                 url=args.url, redirect_to_login=True)

        log(f"页面：{info.get('title')}（正文 {info.get('textLen')} 字符）")
        hidden = is_hidden(info.get("text", ""))
        replied = False
        reply_note = ""

        if hidden:
            log("检测到隐藏内容，准备自动回复…")
            if not info.get("textareas"):
                reply_note = "页面上没有回复输入框，可能未登录或无权限回复"
                log(f"  {reply_note}")
            else:
                js = REPLY_JS_TMPL % json.dumps(args.text, ensure_ascii=False)
                res = json.loads(cdp.eval_js(tid, js))
                log(f"  回复结果：{res}")
                if res.get("ok"):
                    replied = True
                    time.sleep(4)
                    # 回复后重新拉一遍页面，隐藏内容才会渲染出来
                    cdp.navigate(tid, args.url)
                    time.sleep(2.5)
                    cdp.wait_ready(tid, timeout=30)
                    time.sleep(1.5)
                    info = json.loads(cdp.eval_js(tid, POST_INFO_JS))
                    hidden = is_hidden(info.get("text", ""))
                else:
                    reply_note = res.get("reason", "回复失败")

        downloads = parse_downloads(info.get("text", ""), info.get("links", []))

        # 正文里抓「作词/作曲」信息，供后续写标签时补全 composer
        text = info.get("text", "")
        composer = ""
        for pat in (r"作曲[：:\s]*([^\n\r|]{1,40})", r"曲[：:]\s*([^\n\r|]{1,40})"):
            m = re.search(pat, text)
            if m:
                composer = re.split(r"\s{2,}|作词|编曲|专辑|歌手", m.group(1))[0].strip()
                break

        emit({
            "ok": True,
            "url": args.url,
            "title": info.get("title"),
            "hidden": hidden,
            "replied": replied,
            "reply_note": reply_note or None,
            "composer_from_post": composer or None,
            "downloads": downloads,
            "text_excerpt": text[:1200],
        })
    finally:
        cdp.close_tab(tid)


def cmd_probe(args) -> None:
    """调试用：dump 页面结构，便于校准选择器。"""
    tid = cdp.new_tab(args.url)
    try:
        cdp.wait_ready(tid, timeout=40)
        time.sleep(2)
        info = json.loads(cdp.eval_js(tid, POST_INFO_JS))
        out = session_dir("_probe") / "page.json"
        out.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        emit({"ok": True, "saved": str(out), "title": info.get("title"),
              "textLen": info.get("textLen"), "textareas": len(info.get("textareas", [])),
              "links": len(info.get("links", []))})
    finally:
        cdp.close_tab(tid)


def main() -> None:
    ap = argparse.ArgumentParser(description="hifini 搜索与取链")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_s = sub.add_parser("search")
    p_s.add_argument("--query", required=True)

    p_r = sub.add_parser("resolve")
    p_r.add_argument("--url", required=True)
    p_r.add_argument("--text", default="谢谢分享，感谢楼主！")

    p_p = sub.add_parser("probe")
    p_p.add_argument("--url", required=True)

    args = ap.parse_args()
    {"search": cmd_search, "resolve": cmd_resolve, "probe": cmd_probe}[args.cmd](args)


if __name__ == "__main__":
    main()
