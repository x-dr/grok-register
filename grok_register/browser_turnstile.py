#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloudflare Turnstile handling for browser registration (from mytest/turnstile.py).

所有函数接受 page 作为参数，不依赖全局变量。
"""

import random
import time

from .browser_js import page_run_js, raise_if_cancelled, sleep_with_cancel, BrowserDeadError

_TURNSTILE_CHECKBOX_OFFSET_X = 26
_TURNSTILE_MIN_W = 50
_TURNSTILE_MIN_H = 20
_CF_CHALLENGE_SRC = "challenges.cloudflare.com"


def _is_turnstile_frame(frame) -> bool:
    try:
        url = frame.url or ""
        name = frame.name or ""
    except Exception:
        return False
    return (
        _CF_CHALLENGE_SRC in url
        or "turnstile" in url.lower()
        or name.startswith("cf-chl-widget")
    )


def _get_shadow_roots(queryable):
    """收集 queryable 内全部 shadow root（依赖 forceScopeAccess → shadowRootUnl）。"""
    if queryable is None:
        return []
    js = """
() => {
  const roots = [];
  function collect(node) {
    if (!node) return;
    const sr = node.shadowRootUnl || node.shadowRoot;
    if (sr) { roots.push(sr); collect(sr); }
    try {
      for (const el of node.querySelectorAll('*')) {
        const child = el.shadowRootUnl || el.shadowRoot;
        if (child) collect(el);
      }
    } catch (e) {}
  }
  collect(document);
  return roots;
}
"""
    try:
        handle = queryable.evaluate_handle(js)
        return _handles_from_js_array(handle)
    except Exception:
        return []


def _handles_from_js_array(handle):
    if handle is None:
        return []
    elements = []
    try:
        props = handle.get_properties()
    except Exception:
        return []
    for prop in props.values():
        try:
            el = prop.as_element()
            if el is not None:
                elements.append(el)
        except Exception:
            continue
    return elements


def _search_shadow_root_elements(queryable, selector: str):
    import json as _json
    found = []
    if queryable is None or not selector:
        return found
    sel_json = _json.dumps(selector)
    for root in _get_shadow_roots(queryable):
        try:
            handle = root.evaluate_handle(f"(shadow) => shadow.querySelector({sel_json})")
            if handle is None:
                continue
            el = handle.as_element()
            if el is not None:
                found.append(el)
        except Exception:
            continue
    try:
        for el in queryable.query_selector_all(selector):
            found.append(el)
    except Exception:
        pass
    return found


def _search_cf_challenge_frames(page):
    if page is None:
        return []
    frames = []
    seen_ids = set()

    def _add_frame(fr):
        if fr is None:
            return
        try:
            if fr.is_detached():
                return
        except Exception:
            pass
        try:
            key = id(fr)
            if key in seen_ids:
                return
            seen_ids.add(key)
        except Exception:
            pass
        frames.append(fr)

    # Shadow 内 iframe → content_frame
    try:
        for iframe_el in _search_shadow_root_elements(page, "iframe"):
            try:
                src = ""
                try:
                    src = str(iframe_el.get_attribute("src") or "")
                except Exception:
                    src = ""
                if src and _CF_CHALLENGE_SRC not in src and "turnstile" not in src.lower():
                    continue
                fr = iframe_el.content_frame()
                if fr is not None and (not src or _is_turnstile_frame(fr) or _CF_CHALLENGE_SRC in src):
                    _add_frame(fr)
            except Exception:
                continue
    except Exception:
        pass

    # page.frames 全量扫描
    try:
        for fr in list(page.frames):
            if _is_turnstile_frame(fr):
                _add_frame(fr)
    except Exception:
        pass

    return frames


def _find_ready_turnstile_checkbox(page, cf_frames, *, attempts=4, delay=0.7):
    for _ in range(max(1, int(attempts))):
        for fr in cf_frames:
            try:
                if fr.is_detached():
                    continue
            except Exception:
                pass
            try:
                checkboxes = _search_shadow_root_elements(fr, 'input[type="checkbox"]')
                if not checkboxes:
                    try:
                        loc = fr.locator('input[type="checkbox"]').first
                        handle = loc.element_handle(timeout=400)
                        if handle is not None:
                            checkboxes = [handle]
                    except Exception:
                        checkboxes = []
                for cb in checkboxes:
                    try:
                        if cb.is_visible():
                            return fr, cb
                    except Exception:
                        return fr, cb
            except Exception:
                continue
        if attempts > 1:
            time.sleep(delay)
    return None


def _turnstile_success_in_frame(frame) -> bool:
    if frame is None:
        return False
    try:
        els = _search_shadow_root_elements(frame, 'div[id="success"]')
        if els:
            return True
    except Exception:
        pass
    try:
        loc = frame.locator("#success, div[id='success']").first
        return loc.count() > 0 and loc.is_visible()
    except Exception:
        return False


def _read_turnstile_token(page):
    try:
        token = page_run_js(page, """
try {
  const inputs = Array.from(document.querySelectorAll(
    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], input[name="g-recaptcha-response"]'
  ));
  for (const n of inputs) {
    const v = String(n.value || '').trim();
    if (v.length >= 20) return v;
  }
  const nodes = Array.from(document.querySelectorAll('input[name*="turnstile"], textarea[name*="turnstile"]'));
  for (const n of nodes) {
    const v = String(n.value || '').trim();
    if (v.length >= 80) return v;
  }
  if (window.turnstile && typeof turnstile.getResponse === 'function') {
    const r = String(turnstile.getResponse() || '').trim();
    if (r) return r;
  }
  return '';
} catch(e) { return ''; }
""")
        return str(token or "").strip()
    except Exception:
        return ""


def _turnstile_solved(page) -> bool:
    token = _read_turnstile_token(page)
    if len(token) >= 80:
        return True
    for fr in _search_cf_challenge_frames(page):
        if _turnstile_success_in_frame(fr):
            return True
    return False


def _element_viewport_box(el):
    if el is None:
        return None
    try:
        el.scroll_into_view_if_needed(timeout=1000)
    except Exception:
        pass
    try:
        rect = el.evaluate("""(e) => {
  if (!e || typeof e.getBoundingClientRect !== 'function') return null;
  const r = e.getBoundingClientRect();
  return { x: r.x, y: r.y, width: r.width, height: r.height };
}""")
        if rect:
            return rect
    except Exception:
        pass
    return None


def _locate_turnstile_widget(page):
    """定位 Turnstile widget 的视口坐标和尺寸。"""
    cf_frames = _search_cf_challenge_frames(page)
    for fr in cf_frames:
        try:
            if fr.is_detached():
                continue
        except Exception:
            pass
        try:
            frame_element = fr.frame_element()
            box = _element_viewport_box(frame_element)
            if box and box.get("width", 0) >= _TURNSTILE_MIN_W and box.get("height", 0) >= _TURNSTILE_MIN_H:
                return {"found": "frame", "x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}
        except Exception:
            continue
    return None


def _fallback_click_iframe_relative(page, *, attempt=0, offset_x=None):
    """iframe 相对坐标点击。"""
    cf_frames = _search_cf_challenge_frames(page)
    ox = offset_x if offset_x is not None else _TURNSTILE_CHECKBOX_OFFSET_X
    oy = 14
    for fr in cf_frames:
        try:
            if fr.is_detached():
                continue
        except Exception:
            pass
        # 先找 checkbox element，用 element.click(position=...)
        try:
            checkboxes = _search_shadow_root_elements(fr, 'input[type="checkbox"]')
            for cb in checkboxes:
                try:
                    cb.click(timeout=1500)
                    return True
                except Exception:
                    pass
                try:
                    cb.click(timeout=1200, force=True)
                    return True
                except Exception:
                    pass
        except Exception:
            pass
        # 再尝试 frame locator 点击
        try:
            fr.locator("body").click(position={"x": ox, "y": oy}, timeout=900, force=True)
            return True
        except Exception:
            pass
    return False


def _fallback_click_mouse_xy(page, *, attempt=0, offset_x=None, log_callback=None):
    """mouse 绝对坐标点击（提前 move 再 click，轨迹更自然）。"""
    coords = _locate_turnstile_widget(page)
    if not coords:
        return False
    ox = offset_x if offset_x is not None else _TURNSTILE_CHECKBOX_OFFSET_X
    oy = 14
    vw = vh = 0
    try:
        vw = float((page.viewport_size or {}).get("width") or 0) or float(page.evaluate("window.innerWidth || 0") or 0) or 1280.0
        vh = float((page.viewport_size or {}).get("height") or 0) or float(page.evaluate("window.innerHeight || 0") or 0) or 720.0
    except Exception:
        vw, vh = 1280.0, 720.0
    tx = max(2.0, min(vw - 2.0, coords["x"] + ox))
    ty = max(2.0, min(vh - 2.0, coords["y"] + oy))
    try:
        page.mouse.move(tx, ty)
        page.mouse.click(tx, ty, delay=random.randint(40, 90))
        return True
    except Exception:
        try:
            page.mouse.click(tx, ty)
            return True
        except Exception:
            return False


def _fallback_click_inside_iframe_js(page, log_callback=None):
    """在 CF iframe 内部伪造 MouseEvent 坐标后直接 click shadow DOM 内 input。"""
    cf_frames = _search_cf_challenge_frames(page)
    for fr in cf_frames:
        try:
            if fr.is_detached():
                continue
        except Exception:
            pass
        try:
            clicked = fr.evaluate("""
(function() {
  try {
    function getRandomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
    var sx = getRandomInt(800, 1200);
    var sy = getRandomInt(400, 700);
    try {
      Object.defineProperty(MouseEvent.prototype, 'screenX', { value: sx, configurable: true });
      Object.defineProperty(MouseEvent.prototype, 'screenY', { value: sy, configurable: true });
    } catch(e) {}
    var roots = [document];
    (function collect(node) {
      try {
        var sr = node.shadowRootUnl || node.shadowRoot;
        if (sr) roots.push(sr);
        var els = node.querySelectorAll('*');
        for (var i = 0; i < els.length; i++) {
          var child = els[i].shadowRootUnl || els[i].shadowRoot;
          if (child) collect(els[i]);
        }
      } catch(e) {}
    })(document);
    for (var r = 0; r < roots.length; r++) {
      try {
        var inp = roots[r].querySelector('input[type="checkbox"]') || roots[r].querySelector('input');
        if (inp) { inp.click(); return true; }
      } catch(e) {}
    }
    return false;
  } catch(e) { return false; }
})();
""")
            if clicked:
                if log_callback:
                    log_callback("[Debug] Turnstile: iframe 内部 JS 点击成功（伪造 MouseEvent）")
                return True
        except Exception:
            continue
    return False


def _click_turnstile_widget(page, log_callback=None, *, attempt=0, offset_x=None):
    """用鼠标点击 Turnstile（humanize 自然轨迹）。"""
    if page is None:
        return {"ok": False, "reason": "no-page"}

    token = _read_turnstile_token(page)
    if len(token) >= 80:
        return {"ok": True, "reason": "already-solved", "token_len": len(token)}

    cf_frames = _search_cf_challenge_frames(page)
    if cf_frames and log_callback and attempt == 0:
        log_callback(f"[Debug] Turnstile: 发现 {len(cf_frames)} 个 CF frame")

    # 路径 1：iframe 相对 position
    if _fallback_click_iframe_relative(page, attempt=attempt, offset_x=offset_x):
        if log_callback:
            log_callback(f"[Debug] Turnstile: iframe 相对坐标已点击 attempt={attempt}")
        return {"ok": True, "reason": "iframe-relative", "attempt": attempt}

    # 路径 2：mouse 绝对坐标
    if _fallback_click_mouse_xy(page, attempt=attempt, offset_x=offset_x, log_callback=log_callback):
        return {"ok": True, "reason": "mouse-xy", "attempt": attempt}

    # 路径 3：iframe 内部 JS 伪造 MouseEvent
    if _fallback_click_inside_iframe_js(page, log_callback=log_callback):
        return {"ok": True, "reason": "iframe-js", "attempt": attempt}

    if log_callback:
        log_callback("[Debug] Turnstile: 未找到可点 widget（可能尚未渲染或 Managed 已自动过）")
    return {"ok": False, "reason": "not-found"}


def get_turnstile_token(page, log_callback=None, cancel_callback=None, *, reset=False, timeout=40):
    """等待 Turnstile 自然通过。只真实点击 checkbox，不伪造/回填 token。"""
    if page is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    if reset:
        try:
            page_run_js(page, "try { if (window.turnstile && typeof turnstile.reset === 'function') turnstile.reset(); } catch(e) {}")
        except Exception:
            pass

    deadline = time.monotonic() + max(1, timeout)
    last_progress_log = 0.0
    click_attempt = 0
    max_clicks = 8
    last_click_at = 0.0
    widget_seen = False

    sleep_with_cancel(0.3, cancel_callback)

    while time.monotonic() < deadline:
        raise_if_cancelled(cancel_callback)

        try:
            token = _read_turnstile_token(page)
            if len(token) >= 80:
                if log_callback:
                    log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                return token
            if _turnstile_solved(page):
                for _ in range(8):
                    token = _read_turnstile_token(page)
                    if len(token) >= 80:
                        if log_callback:
                            log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                        return token
                    sleep_with_cancel(0.35, cancel_callback)
        except Exception:
            pass

        now = time.monotonic()
        remaining = max(0.0, deadline - now)

        if log_callback and now - last_progress_log >= 5:
            try:
                n_frames = len(_search_cf_challenge_frames(page))
            except Exception:
                n_frames = 0
            log_callback(f"[Debug] Turnstile 等待中，剩余约 {int(remaining)}s frames={n_frames}")
            last_progress_log = now

        should_click = (
            click_attempt < max_clicks
            and remaining > 2.0
            and (click_attempt == 0 or now - last_click_at >= 2.0)
        )
        if should_click:
            try:
                result = _click_turnstile_widget(page, log_callback=log_callback, attempt=click_attempt)
                last_click_at = time.monotonic()
                click_attempt += 1
                if result.get("reason") == "already-solved":
                    token = _read_turnstile_token(page)
                    if len(token) >= 80:
                        if log_callback:
                            log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                        return token
                if result.get("ok"):
                    widget_seen = True
                    sleep_with_cancel(0.6, cancel_callback)
                    poll_deadline = time.monotonic() + min(3.0, max(1.0, deadline - time.monotonic()))
                    while time.monotonic() < poll_deadline:
                        raise_if_cancelled(cancel_callback)
                        token = _read_turnstile_token(page)
                        if len(token) >= 80:
                            if log_callback:
                                log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                            return token
                        if _turnstile_solved(page):
                            token = _read_turnstile_token(page)
                            if len(token) >= 80:
                                if log_callback:
                                    log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                                return token
                        sleep_with_cancel(0.45, cancel_callback)
            except Exception as click_exc:
                if log_callback:
                    log_callback(f"[Debug] Turnstile 点击异常: {click_exc}")
                last_click_at = time.monotonic()
                click_attempt += 1

        sleep_with_cancel(min(0.6, max(0.0, deadline - time.monotonic())), cancel_callback)

    hint = "widget曾出现" if widget_seen else "全程未定位到widget"
    raise Exception(f"Turnstile 获取 token 失败 ({hint}, 点击{click_attempt}次)")
