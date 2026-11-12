"""web-access CDP Proxy 的轻量客户端。

hifini.com.cn 有 JS 反爬（首页只返回 <script src="/_guard/auto.js">），静态抓取拿不到内容，
必须用真实浏览器。这里通过 web-access skill 的 CDP Proxy（默认 localhost:3456）
复用用户日常 Chrome 的登录态，在自己创建的标签页里操作，结束后关闭。
"""
from __future__ import annotations

import json
import time
from urllib.parse import quote

import requests

PROXY = "http://localhost:3456"
TIMEOUT = 60


class CDPError(RuntimeError):
    pass


def _check_proxy() -> None:
    try:
        requests.get(f"{PROXY}/targets", timeout=5)
    except requests.RequestException as exc:
        raise CDPError(
            "CDP Proxy 未运行。请先在终端执行：\n"
            '  node "/Users/wttch/.workbuddy/skills/web-access/scripts/check-deps.mjs"\n'
            "并在 Chrome 地址栏打开 chrome://inspect/#remote-debugging，"
            "勾选 Allow remote debugging for this browser instance。"
        ) from exc


def new_tab(url: str) -> str:
    """开一个后台标签页并加载 URL，返回 targetId。"""
    _check_proxy()
    r = requests.post(f"{PROXY}/new", data=url.encode(), timeout=TIMEOUT)
    payload = r.text.strip()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        # 有些版本直接返回 targetId 字符串
        return payload.strip('"')
    tid = data.get("targetId") or data.get("id") or data.get("target")
    if not tid:
        raise CDPError(f"创建标签页失败：{payload[:300]}")
    return tid


def close_tab(tid: str) -> None:
    try:
        requests.get(f"{PROXY}/close", params={"target": tid}, timeout=15)
    except requests.RequestException:
        pass


def navigate(tid: str, url: str) -> None:
    requests.post(f"{PROXY}/navigate", params={"target": tid}, data=url.encode(), timeout=TIMEOUT)


def eval_js(tid: str, js: str) -> object:
    """在页面里执行 JS。返回解析后的值；字符串原样返回。"""
    r = requests.post(f"{PROXY}/eval", params={"target": tid}, data=js.encode(), timeout=TIMEOUT)
    text = r.text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(data, dict):
        if data.get("error"):
            raise CDPError(str(data["error"]))
        for key in ("result", "value", "data"):
            if key in data:
                return data[key]
    return data


def wait_ready(tid: str, extra_check: str | None = None, timeout: int = 40) -> None:
    """等页面加载完成，可选附加一个 JS 条件（返回真值表示就绪）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = eval_js(tid, "document.readyState")
            if state == "complete":
                if not extra_check:
                    return
                if eval_js(tid, extra_check):
                    return
        except CDPError:
            pass
        time.sleep(1)
    # 超时不抛错：交给调用方用内容判断，很多页面永远不会满足理想条件


def fetch_text(tid: str, url: str, wait_ms: int = 1500) -> str:
    """在当前标签页导航并返回 body 文本。"""
    navigate(tid, url)
    time.sleep(wait_ms / 1000)
    return str(eval_js(tid, "document.body ? document.body.innerText : ''"))


def encode(s: str) -> str:
    return quote(s, safe="")


# 注入式提取脚本：在页面里跑，返回结构化数据
EXTRACT_LINKS_JS = r"""
(() => {
  const abs = u => { try { return new URL(u, location.href).href } catch(e) { return u } };
  const items = [];
  document.querySelectorAll('a[href]').forEach(a => {
    const href = abs(a.getAttribute('href'));
    const text = (a.textContent || '').trim().replace(/\s+/g, ' ');
    if (!text || text.length < 2) return;
    items.push({href, text, title: a.getAttribute('title') || ''});
  });
  return JSON.stringify({url: location.href, title: document.title, links: items});
})()
"""
