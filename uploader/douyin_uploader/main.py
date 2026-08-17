# -*- coding: utf-8 -*-
from datetime import datetime

import asyncio
import inspect
import os
import re
import time
from pathlib import Path

from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import async_playwright

from conf import DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.login_qrcode import build_login_qrcode_path
from utils.login_qrcode import decode_qrcode_from_path
from utils.login_qrcode import print_terminal_qrcode
from utils.login_qrcode import remove_qrcode_file
from utils.login_qrcode import save_data_url_image
from utils.log import douyin_logger

DOUYIN_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
DOUYIN_PUBLISH_STRATEGY_SCHEDULED = "scheduled"

# 登录态 cookie 名（扫码/校验/上传共用）
DOUYIN_SESSION_COOKIE_NAMES = frozenset(
    {"sessionid", "sessionid_ss", "sid_tt", "uid_tt", "passport_auth_status"}
)


def _douyin_browser_launch_kwargs(headless: bool) -> dict:
    """统一 Chrome 启动参数，避免校验用 chrome、上传用 chromium 导致 cookie 行为不一致"""
    return {
        "headless": headless,
        "channel": "chrome",
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    }


def _storage_state_has_douyin_session(account_file: str) -> bool:
    """从已保存的 storage_state 判断是否含有效登录 session"""
    import json

    path = Path(account_file)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    for cookie in data.get("cookies", []):
        if cookie.get("name") in DOUYIN_SESSION_COOKIE_NAMES and cookie.get("value"):
            return True
    return False


def _cookies_have_douyin_session(cookies: list) -> bool:
    return any(c.get("name") in DOUYIN_SESSION_COOKIE_NAMES and c.get("value") for c in cookies)


def _is_douyin_login_landing_url(url: str) -> bool:
    """是否仍停留在扫码登录落地页（仅此类页面才应刷新二维码）"""
    if "creator.douyin.com/creator-micro" in url:
        return False
    if "creator.douyin.com" in url:
        return True
    lowered = url.lower()
    return "passport" in lowered or "/login" in lowered


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    if not qrcode_callback:
        return

    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_login_result(success: bool, status: str, message: str, account_file: str, qrcode: dict | None = None, current_url: str = "") -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


async def _find_upload_file_input(page: Page):
    """定位上传页视频 file input，排除登录表单里的 input"""
    selectors = (
        "div[class^='container'] input[type='file']",
        "div[class^='upload'] input[type='file']",
        "input[type='file'][accept*='video']",
        "input[type='file'][accept*='mp4']",
    )
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = await loc.count()
            for idx in range(count):
                item = loc.nth(idx)
                if not await item.count():
                    continue
                # 上传控件常为 hidden input，attached 即可；须排除 web-login 容器
                parent_html = await item.evaluate(
                    "el => (el.closest('[class*=\"web-login\"]') || el.closest('[class*=\"login\"]'))?.className || ''"
                )
                if parent_html and "web-login" in str(parent_html):
                    continue
                return item
        except Exception:
            continue
    return page.locator("div[class^='container'] input[type='file']").first


def _upload_video_button(page: Page):
    """新版创作者中心拖拽区主按钮：文案「上传视频」（可能拆成两个文本节点）。"""
    return page.get_by_role("button", name=re.compile(r"上传\s*视频"))


async def _douyin_upload_button_visible(page: Page) -> bool:
    try:
        loc = _upload_video_button(page)
        return bool(await loc.count()) and await loc.first.is_visible()
    except Exception:
        return False


async def _set_douyin_upload_file(page: Page, file_path: str) -> None:
    """优先点击可见「上传视频」走 file chooser；失败再回退 hidden input。"""
    btn = _upload_video_button(page)
    click_targets = [
        btn.first,
        page.locator('[class*="container-drag-btn"]').first,
        page.locator("button.semi-button-primary").filter(has_text="上传视频").first,
        page.get_by_text("点击上传 或直接将视频文件拖入此区域").first,
    ]
    for idx, target in enumerate(click_targets):
        try:
            if not await target.count():
                continue
            if not await target.is_visible():
                continue
            async with page.expect_file_chooser(timeout=8000) as fc_info:
                await target.click(force=True)
            chooser = await fc_info.value
            await chooser.set_files(file_path)
            douyin_logger.info(_msg("📤", f"已点击「上传视频」并写入文件（target#{idx}）"))
            return
        except Exception as exc:
            douyin_logger.debug(_msg("🔍", f"上传按钮 fileChooser target#{idx} 失败: {exc}"))

    upload_input = await _find_upload_file_input(page)
    douyin_logger.info(_msg("📤", "未点到「上传视频」，回退写入 hidden file input"))
    await upload_input.wait_for(state="attached", timeout=120000)
    await upload_input.set_input_files(file_path)


async def _try_click_upload_page_next(page: Page) -> bool:
    """上传完成后若仍停在 upload 页，尝试点「下一步/发布」。"""
    names = ("下一步", "进入发布", "去发布", "发布")
    for name in names:
        try:
            loc = page.get_by_role("button", name=name, exact=True)
            if await loc.count() and await loc.first.is_visible():
                await loc.first.click()
                douyin_logger.info(_msg("👉", f"上传页点击「{name}」"))
                return True
        except Exception:
            continue
    return False


async def _douyin_upload_input_visible(page: Page) -> bool:
    """上传页 file input 是否已挂载（比登录 Tab 文案更可靠）"""
    try:
        loc = await _find_upload_file_input(page)
        return bool(await loc.count())
    except Exception:
        return False


async def _douyin_page_has_login_prompt(page: Page) -> bool:
    """检测页面是否仍处于登录态（含慢加载时的登录文案）"""
    # 抖音上传页 DOM 常残留「扫码登录/密码登录」Tab，但 file input / 上传按钮已可见说明会话有效
    if await _douyin_upload_button_visible(page) or await _douyin_upload_input_visible(page):
        return False

    login_texts = ("手机号登录", "扫码登录", "密码登录")
    for text in login_texts:
        try:
            loc = page.get_by_text(text)
            count = await loc.count()
            for idx in range(count):
                item = loc.nth(idx)
                if await item.is_visible():
                    return True
        except Exception:
            continue
    return False


async def _douyin_upload_page_ready(page: Page) -> bool:
    """上传页就绪：URL 正确且出现上传按钮或 file input"""
    if "content/upload" not in page.url:
        return False
    if await _douyin_page_has_login_prompt(page):
        return False
    return await _douyin_upload_button_visible(page) or await _douyin_upload_input_visible(page)


# 抖音新版发布页 URL：离开该页面即视为发布成功（抖音改版后不一定跳到 content/manage，可能跳数据中心/首页）
_DOUYIN_PUBLISH_PAGE_URL_PATTERNS = (
    "/content/publish",   # version_1 发布页
    "/content/post/video", # version_2 发布页
    "/content/post/note",  # 图文发布页
    "/content/upload",     # 上传页
)


def _douyin_left_publish_page(page: Page) -> bool:
    """判断是否已离开发布/上传页（发布成功的宽松判定，兼容抖音改版跳数据中心/首页）"""
    url = page.url or ""
    for pattern in _DOUYIN_PUBLISH_PAGE_URL_PATTERNS:
        if pattern in url:
            return False
    # 仍在 creator.douyin.com 且不匹配任何发布/上传页 → 已跳转（数据中心/内容管理皆算成功）
    return "creator.douyin.com" in url


async def _douyin_click_next_step_button(page: Page) -> bool:
    """点击「下一步」按钮：抖音两阶段发布页（填写信息 → 下一步 → 确认/发布），需先进入最终发布页。"""
    selectors = [
        "button:has-text('下一步')",
        "[role='button']:has-text('下一步')",
        "button:has-text('继续')",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            cnt = await loc.count()
            for idx in range(cnt):
                b = loc.nth(idx)
                if not (await b.is_visible()) or await b.is_disabled():
                    continue
                txt = (await b.text_content() or "").strip()
                if txt not in ("下一步", "继续"):
                    continue
                try:
                    await b.click(force=True, timeout=5000)
                    return True
                except Exception:
                    try:
                        await b.evaluate("el => el.click()")
                        return True
                    except Exception:
                        pass
        except Exception:
            continue
    return False


async def _douyin_click_publish_button(page: Page) -> bool:
    """点击底部主「发布」按钮，多选择器兜底（2026-08-13 新版 DOM）。

    顺序（优先级从高到低，每条都先排除非主发布文案）：
      1. 新版确认栏：#popover-tip-container > button（文案「发布」，旁边是「暂存离开」）
      2. [class*='content-confirm-container'] 内的 primary 按钮
      3. footer / fixed-bottom 区域内的 primary 按钮（旧版）
      4. get_by_role('button', name='发布', exact=True) + 文案精确 == "发布"
      5. CSS class：button- + primary（兼容 primary--hash 与 primary-hash）
      6. 纯文字兜底 + JS DOM click（popover 遮罩时 Playwright 可能点不到）
    只要任意一种方式点过就返回 True（按钮不可见仍返回 False）。

    ⚠️ 严禁使用 startswith/模糊匹配：必须 txt == "发布"，避免点到「发布设置」「发布视频」
       「取消发布」「暂存离开」等，它们一旦被点击会跳走，表现为未点发布却已离开发布页。
    """

    def _txt_ok(txt: str) -> bool:
        s = "".join((txt or "").split())
        if not s:
            return False
        if any(
            kw in s
            for kw in (
                "设置", "时间", "定时", "草稿", "预览", "规则", "须知",
                "商品", "合集", "取消", "暂存", "离开", "下一步",
            )
        ):
            return False
        return s == "发布"

    async def _cls_ok(b) -> bool:
        try:
            cls = (await b.get_attribute("class") or "").lower()
            if "cancel" in cls:
                return False
        except Exception:
            pass
        return True

    async def _try_click(b) -> bool:
        try:
            await b.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            await b.click(force=True, timeout=4000)
            return True
        except Exception:
            pass
        try:
            await b.evaluate("el => el.click()")
            return True
        except Exception:
            return False

    async def _try_locators(locator, desc: str) -> bool:
        try:
            cnt = await locator.count()
        except Exception:
            return False
        for idx in range(cnt):
            b = locator.nth(idx)
            try:
                if not (await b.is_visible()) or await b.is_disabled():
                    continue
                if not await _cls_ok(b):
                    continue
                if not _txt_ok(await b.text_content() or ""):
                    continue
                if await _try_click(b):
                    douyin_logger.info(_msg("👆", f"命中发布按钮选择器: {desc}"))
                    return True
            except Exception:
                continue
        return False

    # 底部确认栏可能是滚动懒加载，先滚到底再找按钮
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
    except Exception:
        pass

    # 1) 2026-08-13 新版：#popover-tip-container > button.button-*.primary-*.fixed-*
    for sel, desc in (
        ("#popover-tip-container > button", "#popover-tip-container"),
        (
            "[class*='content-confirm-container'] button[class*='primary']",
            "content-confirm-container primary",
        ),
        (
            "[class*='content-confirm-container'] button[class*='button-']",
            "content-confirm-container button",
        ),
        (
            "[class*='new-layout'] button[class*='primary'][class*='fixed-']",
            "new-layout primary fixed",
        ),
    ):
        if await _try_locators(page.locator(sel), desc):
            return True

    # 2) 旧版底部固定区域（primary- 可同时匹配 primary--hash 与 primary-hash）
    for sel, desc in (
        ("[class*='footer'] button[class*='primary']", "footer primary"),
        ("[class*='fixed-bottom'] button[class*='primary']", "fixed-bottom primary"),
        ("form button[class*='primary']:last-of-type", "form last primary"),
    ):
        if await _try_locators(page.locator(f"{sel}:has-text('发布')"), desc):
            return True

    # 3) 文案驱动，exact=True 避免匹配「发布设置/发布视频」
    try:
        if await _try_locators(
            page.get_by_role("button", name="发布", exact=True),
            "role=button exact",
        ):
            return True
    except Exception:
        pass

    # 4) Class 前缀：button-xxxx primary-yyyy / primary--yyyy
    if await _try_locators(
        page.locator("button[class*='primary'][class*='button-']:has-text('发布')"),
        "button.primary.button-",
    ):
        return True

    # 5) 纯文字兜底：必须限定在 button / [role=button] 内，且文案精确 == 发布
    if await _try_locators(
        page.locator("button:has-text('发布'), [role='button']:has-text('发布')"),
        "button has-text 发布",
    ):
        return True

    # 6) JS 兜底：#popover-tip-container 上的 popover/semi-portal 可能挡住 Playwright 点击
    try:
        clicked_js = await page.evaluate(
            """() => {
                const textOk = (el) => {
                    const s = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
                    return s === '发布';
                };
                const reject = (el) => {
                    const cls = (el.className || '').toString().toLowerCase();
                    const s = (el.innerText || el.textContent || '');
                    if (cls.includes('cancel')) return true;
                    if (s.includes('暂存') || s.includes('离开') || s.includes('设置') || s.includes('取消')) return true;
                    return false;
                };
                const list = [];
                const pop = document.querySelector('#popover-tip-container button');
                if (pop) list.push(pop);
                document.querySelectorAll('[class*="content-confirm-container"] button').forEach((b) => list.push(b));
                document.querySelectorAll('button[class*="primary"]').forEach((b) => list.push(b));
                const seen = new Set();
                for (const el of list) {
                    if (!el || seen.has(el) || reject(el) || !textOk(el) || el.disabled) continue;
                    seen.add(el);
                    const st = window.getComputedStyle(el);
                    if (st.display === 'none' || st.visibility === 'hidden') continue;
                    el.scrollIntoView({ block: 'center', inline: 'center' });
                    el.click();
                    return true;
                }
                return false;
            }"""
        )
        if clicked_js:
            douyin_logger.info(_msg("👆", "命中发布按钮选择器: JS DOM click"))
            return True
    except Exception:
        pass

    return False


async def _douyin_set_self_declaration_entry(page: Page) -> bool:
    """打开自主声明弹窗入口，新老 DOM 兜底 + debug 日志打印（2026-08-08 三改版）。

    按用户最新 DOM 截图（入口位于发布页 DCFF > form-container 底部，上传前即可点击）：
      <div id="DCFF">
        <div class="form-container-MBtobk new-layout">
          ...
          <section class="wrapper-ML2dHB">
            <div class="labelWrapper-p6oS_Jm"/>
            <div class="controlWrapper-Kt_9Km">
              <div class="selectBox-buZkRi">                 ← 驼峰 selectBox
                <div class="selectText-ESsMPZ">请选择自主声明</div>
                <span class="chevron-ewXR1"/>
              </div>
            </div>
          </section>
        </div>
      </div>
    点击成功返回 True；每级失败均打 DEBUG 日志，便于下次定位"到底卡在哪一步"。
    """
    _DEBUG = True  # 临时开关，若日志太啰嗦改 False

    async def _click_with_log(loc, *, tag: str) -> bool:
        try:
            if await loc.count() == 0:
                _DEBUG and douyin_logger.debug(f"[声明入口-{tag}] count=0 未命中")
                return False
            if not await loc.is_visible():
                # 滚到可见（该区域在发布页底部，不滚动会 pointer-events 拦截）
                try:
                    await loc.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                if not await loc.is_visible():
                    _DEBUG and douyin_logger.debug(f"[声明入口-{tag}] 命中但不可见，尝试 force=True 强点")
            try:
                await loc.click(force=True, timeout=4000)
            except Exception:
                # Playwright click 被遮罩拦截 → 退回浏览器原生事件
                await loc.evaluate("el => el.scrollIntoView({block:'center'}); el.click();")
            _DEBUG and douyin_logger.info(f"[声明入口-{tag}] 点击成功")
            return True
        except Exception as exc:
            _DEBUG and douyin_logger.debug(f"[声明入口-{tag}] 异常: {type(exc).__name__}: {exc}")
            return False

    # ======= 第 1 级：DCFF > form-container 区域内强锁定（最新 2026-08-08 改版） =======
    # 按用户 DOM：#DCFF 是发布表单总容器，form-container-* 内的 section.wrapper-* 含自主声明
    # 该方式可避开页面上其他「请选择自主声明」（如草稿箱里的假 DOM）
    scope_selector = ",".join([
        "#DCFF section[class*='wrapper-'] div[class*='controlWrapper-'] div[class*='selectBox-']:has-text('请选择自主声明')",
        "[class*='form-container-'] section[class*='wrapper-'] div[class*='controlWrapper-'] div[class*='selectBox-']:has-text('请选择自主声明')",
    ])
    if await _click_with_log(page.locator(scope_selector).first, tag="1-DCFF/form-container>wrapper>controlWrapper>selectBox"):
        return True

    # ======= 第 2 级：不锁区域，但要求是 section.wrapper-* > .controlWrapper-* > .selectBox-* 三层结构 =======
    if await _click_with_log(
        page.locator(
            "section[class*='wrapper-'] div[class*='controlWrapper-'] div[class*='selectBox-']:has-text('请选择自主声明')"
        ).first,
        tag="2-section.wrapper>controlWrapper>selectBox",
    ):
        return True

    # ======= 第 3 级：.selectBox- 单级（section 名变化但控件 class 不变） =======
    if await _click_with_log(
        page.locator(
            "div[class*='selectBox-']:has(> div[class*='selectText-']:has-text('请选择自主声明'))"
        ).first,
        tag="3-selectBox>selectText",
    ):
        return True

    # ======= 第 4 级：旧 DOM（.select-box- 连字符） =======
    if await _click_with_log(
        page.locator("div[class*='select-box-']:has-text('请选择自主声明')").first,
        tag="4-select-box-(legacy)",
    ):
        return True

    # ======= 第 5 级：publish-mention-wrapper 相邻位置兜底（用户截图该 wrapper 在 section 上面） =======
    if await _click_with_log(
        page.locator(
            "[class*='publish-mention-wrapper'] + * section[class*='wrapper-'] div[class*='selectBox-']:has-text('请选择自主声明'),"
            " [class*='publish-mention-wrapper'] div[class*='selectBox-']:has-text('请选择自主声明')"
        ).first,
        tag="5-publish-mention-wrapper-adjacent",
    ):
        return True

    # ======= 第 6 级：纯文字 + 向上 1 层 DOM =======
    if await _click_with_log(
        page.get_by_text("请选择自主声明").first,
        tag="6-pure-text",
    ):
        return True

    # ======= 第 7 级：JS evaluate 原生 querySelectorAll 兜底（防 CSS 选择器 Playwright 解析异常） =======
    try:
        clicked_js = await page.evaluate("""() => {
            const list = document.querySelectorAll('section[class*="wrapper-"] div[class*="selectBox-"], div[class*="selectBox-"], div[class*="select-box-"]');
            for (const el of list) {
                if (el.textContent && el.textContent.includes('请选择自主声明')) {
                    el.scrollIntoView({block:'center'});
                    el.click();
                    return true;
                }
            }
            return false;
        }""")
        if clicked_js:
            _DEBUG and douyin_logger.info("[声明入口-7-JS_evaluate] 点击成功")
            return True
    except Exception as exc:
        _DEBUG and douyin_logger.debug(f"[声明入口-7-JS_evaluate] 异常: {exc}")

    # 全部失败：打印候选 class
    # ⚠️ 注意：「已选声明」后入口文案会变（如「内容由AI生成」而不是「请选择自主声明」），此时 7 级全失败是正常的，
    # 上层调用应先通过 _douyin_has_self_declaration_pending() 判定是否真的未选。故默认打 DEBUG，不打 WARNING。
    if _DEBUG:
        try:
            cand = await page.evaluate("""() => {
                const pick = (sel, maxN=30) => Array.from(document.querySelectorAll(sel)).slice(0, maxN)
                    .map(e => [e.className ? e.className.toString().slice(0,80) : '[no-class]', e.textContent ? e.textContent.slice(0,20).replace(/\\s+/g,' ') : '']);
                return {
                    sections: pick('section[class*="wrapper-"]'),
                    selects: pick('div[class*="selectBox-"], div[class*="select-box-"]'),
                    form: pick('[class*="form-container-"]', 5).map(x=>x[0]),
                };
            }""")
            douyin_logger.debug(f"[声明入口-ALL] 7 级入口均未命中（若已选声明属正常），候选 class: {cand}")
        except Exception:
            pass
    return False


async def _douyin_has_self_declaration_pending(page: Page) -> bool:
    """是否仍显示「请选择自主声明」（未选则发布按钮会被抖音拦截）。
    与入口 helper 同步使用 「DCFF/form-container 区域内优先 + 新老双写法 + JS兜底」，避免口径不一致。
    """
    # 最宽松先快速判真：有"请选择自主声明"文字且匹配 select 容器
    checkers = [
        # DCFF + 三层结构强锁定
        lambda p: p.locator(
            "#DCFF section[class*='wrapper-'] div[class*='controlWrapper-'] div[class*='selectBox-']:has-text('请选择自主声明')"
        ).count(),
        # 三层结构
        lambda p: p.locator(
            "section[class*='wrapper-'] div[class*='controlWrapper-'] div[class*='selectBox-']:has-text('请选择自主声明')"
        ).count(),
        # selectBox- 驼峰
        lambda p: p.locator("div[class*='selectBox-']:has(> div[class*='selectText-']:has-text('请选择自主声明'))").count(),
        # select-box- 连字符
        lambda p: p.locator("div[class*='select-box-']:has-text('请选择自主声明')").count(),
        # 纯文字
        lambda p: p.get_by_text("请选择自主声明").count(),
    ]
    for fn in checkers:
        try:
            if await fn(page) > 0:
                return True
        except Exception:
            pass
    # JS 兜底
    try:
        if await page.evaluate("""() => {
            const list = document.querySelectorAll('section[class*="wrapper-"] div[class*="selectBox-"], div[class*="selectBox-"], div[class*="select-box-"]');
            for (const el of list) { if (el.textContent && el.textContent.includes('请选择自主声明')) return true; }
            return false;
        }"""):
            return True
    except Exception:
        pass
    return False


async def cookie_auth(account_file):
    # 抖音无头会撞反爬墙→content/upload 跳登录→误判 cookie 失效（间歇性）。校验必须有头。
    # SPA 慢加载/瞬时跳转会导致单次快照误判，故每轮内轮询 + 轮间退避重试。
    # 允许 linux server 用户通过 env var 强制无头: DOUYIN_COOKIE_AUTH_HEADLESS=true
    use_headless = os.environ.get("DOUYIN_COOKIE_AUTH_HEADLESS", "").lower() in ("1", "true", "yes")
    launch_kwargs = _douyin_browser_launch_kwargs(use_headless)
    poll_interval_ms = 2000
    poll_rounds = 6  # 单轮浏览器内最多等待约 12s
    for attempt in range(3):
        if attempt > 0:
            await asyncio.sleep(2 + attempt)  # 轮间退避，降低连续启动浏览器触发风控
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context(storage_state=account_file)
                context = await set_init_script(context)
                page = await context.new_page()
                await page.goto("https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=180000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                for _ in range(poll_rounds):
                    if await _douyin_upload_page_ready(page):
                        return True
                    if "creator.douyin.com" in page.url and "content/upload" not in page.url:
                        break  # 已跳离上传页，本轮不必继续等
                    await page.wait_for_timeout(poll_interval_ms)
            except Exception:
                pass
            finally:
                await browser.close()
    return False


async def douyin_setup(
    account_file,
    handle=False,
    return_detail=False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
    cdp_url: str | None = None,
    poll_interval: int = 2,
    max_checks: int = 60,
    force_login: bool = False,
):
    # 用户主动扫码登录时跳过 cookie 快检，避免旧 cookie 在有头模式下误判有效而秒关窗口
    if force_login and handle:
        douyin_logger.info(_msg("🧍", "强制扫码登录，跳过 cookie 快检"))
        result = await douyin_cookie_gen(
            account_file,
            qrcode_callback=qrcode_callback,
            headless=headless,
            cdp_url=cdp_url,
            poll_interval=poll_interval,
            max_checks=max_checks,
        )
        return result if return_detail else result["success"]

    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        douyin_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器重新登录"))
        result = await douyin_cookie_gen(
            account_file,
            qrcode_callback=qrcode_callback,
            headless=headless,
            cdp_url=cdp_url,
            poll_interval=poll_interval,
            max_checks=max_checks,
        )
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def _extract_douyin_qrcode_src(page: Page) -> str:
    # 等 SPA 加载完成（不只等"扫码登录"文字，否则抖音慢加载时 30s 就超时）。
    # 给 domcontentloaded 后足够时间让客户端 JS 注入登录卡。
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    scan_login_tab = page.get_by_text("扫码登录", exact=True).first
    # attached 状态：DOM 里出现即可，不要求 visible/渲染完整，避免 race
    await scan_login_tab.wait_for(state="attached", timeout=120000)

    # 新版抖音创作者中心 (single_tab + animate_qrcode_container) 不再用 aria-label="二维码"。
    # 按优先级兜底多个 selector，至少一个能命中即可。
    qrcode_selectors = [
        'div#animate_qrcode_container img[src^="data:image"]',
        'div[class*="animate_qrcode_container"] img[src^="data:image"]',
        'div[class*="scan_qrcode_login_content"] img[src^="data:image"]',
        'img[aria-label="二维码"]',
    ]
    last_err: Exception | None = None
    for sel in qrcode_selectors:
        qrcode_img = page.locator(sel).first
        try:
            await qrcode_img.wait_for(state="attached", timeout=10000)
        except Exception as e:
            last_err = e
            continue
        src = await qrcode_img.get_attribute("src")
        if src:
            return src
        last_err = RuntimeError(f"selector {sel} 命中但 src 为空")

    raise RuntimeError(f"未获取到抖音登录二维码地址 (last_err={last_err})")


async def _save_douyin_qrcode(page: Page, account_file: str, previous_qrcode_path: Path | None = None, qrcode_callback=None) -> dict:
    # 提取二维码 src 仅为了保存/终端显示；定位不到时不致命——有头浏览器里二维码可见，直接扫码即可
    try:
        qrcode_src = await _extract_douyin_qrcode_src(page)
    except Exception as exc:
        douyin_logger.warning(_msg("😵", f"没定位到二维码元素（{str(exc)[:50]}）——请直接在弹出的浏览器里扫码，小人继续等登录跳转"))
        return {"image_path": "", "image_data_url": ""}
    qrcode_path = save_data_url_image(qrcode_src, build_login_qrcode_path(account_file))
    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            douyin_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))
    douyin_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "抖音APP")
    else:
        douyin_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))
    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _is_douyin_login_completed(page: Page) -> bool:
    """识别扫码/二次验证后的登录完成（必须含 session cookie，防止未扫码误判）"""
    cookies = await page.context.cookies()
    has_session = _cookies_have_douyin_session(cookies)
    if not has_session:
        return False

    url = page.url
    has_login_prompt = await _douyin_page_has_login_prompt(page)
    if has_login_prompt:
        return False

    if "creator.douyin.com" not in url:
        return False

    # 仍在扫码落地页且二维码可见 → 未完成
    if _is_douyin_login_landing_url(url):
        login_markers = [
            page.get_by_text("扫码登录", exact=True).first,
            page.get_by_text("二维码失效", exact=True).first,
            page.get_by_role("img", name="二维码").first,
        ]
        for marker in login_markers:
            if not await marker.count():
                continue
            try:
                if await marker.is_visible():
                    return False
            except Exception:
                continue

    return True


async def _wait_for_douyin_login(
    page: Page,
    account_file: str,
    qrcode_info: dict,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 100,
    original_url: str = "",
) -> dict:
    if not original_url:
        original_url = page.url
    qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
    saw_2fa = False
    for i in range(max_checks):
        if await _is_douyin_login_completed(page):
            douyin_logger.info(_msg("🥳", f"扫码成功，已经跳转到登录后页面: {page.url}"))
            return _build_login_result(True, "success", "抖音扫码登录成功", account_file, qrcode_info, page.url)

        # URL 已离开登录页但未完成 → 二次验证/跳转中，禁止刷新二维码
        if page.url != original_url and not await _is_douyin_login_completed(page):
            sms_input = page.locator(
                'input[placeholder*="验证码"], input[type="tel"], input[placeholder*="短信"], input[placeholder*="手机号"]'
            )
            verify_texts = ("安全验证", "身份验证", "人脸识别", "短信验证码", "请完成验证")
            saw_verify_ui = await sms_input.count() > 0
            if not saw_verify_ui:
                for text in verify_texts:
                    if await page.get_by_text(text).count():
                        saw_verify_ui = True
                        break
            if saw_verify_ui and not saw_2fa:
                douyin_logger.warning(
                    _msg("⚠️", f"检测到抖音二次验证，请在浏览器中手动完成（{i + 1}/{max_checks}）")
                )
                saw_2fa = True
            await asyncio.sleep(poll_interval)
            continue

        # 仅仍在登录落地页时才刷新失效二维码，避免扫码后误触「再扫一次」
        if _is_douyin_login_landing_url(page.url):
            expired_box = page.get_by_text("二维码失效", exact=True).locator("..").first
            if await expired_box.count() and await expired_box.is_visible():
                douyin_logger.warning(_msg("😵", "二维码失效了，小人马上去刷新"))
                await expired_box.click()
                await asyncio.sleep(1)
                qrcode_info = await _save_douyin_qrcode(page, account_file, qrcode_path, qrcode_callback=qrcode_callback)
                qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None

        await asyncio.sleep(poll_interval)

    return _build_login_result(False, "timeout", "等待抖音扫码登录超时", account_file, qrcode_info, page.url)


async def douyin_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 2,
    max_checks: int = 60,
    headless: bool = LOCAL_CHROME_HEADLESS,
    cdp_url: str | None = None,
):
    async with async_playwright() as playwright:
        if cdp_url:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            should_close_context = False
        else:
            browser = await playwright.chromium.launch(**_douyin_browser_launch_kwargs(headless))
            context = await browser.new_context()
            should_close_context = True
        context = await set_init_script(context)
        qrcode_path = None
        result = _build_login_result(False, "failed", "抖音登录失败", account_file)
        try:
            page = await context.new_page()
            await page.goto("https://creator.douyin.com/")
            original_url = page.url
            qrcode_info = await _save_douyin_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
            douyin_logger.info(_msg("🧍", "请扫码，小人正在耐心等待登录完成"))
            result = await _wait_for_douyin_login(
                page,
                account_file,
                qrcode_info,
                qrcode_callback=qrcode_callback,
                poll_interval=poll_interval,
                max_checks=max_checks,
                original_url=original_url,
            )
            if result["success"]:
                await asyncio.sleep(2)
                # 登录后先进入上传页再保存，确保 cookie + localStorage 对发布页有效
                upload_ready = False
                try:
                    await page.goto(
                        "https://creator.douyin.com/creator-micro/content/upload",
                        wait_until="domcontentloaded",
                        timeout=180000,
                    )
                    for wait_idx in range(30):
                        if await _douyin_upload_page_ready(page):
                            upload_ready = True
                            douyin_logger.info(_msg("✅", f"登录后上传页就绪（{wait_idx + 1}s）"))
                            break
                        if await _douyin_page_has_login_prompt(page):
                            break
                        await asyncio.sleep(2)
                except Exception as exc:
                    douyin_logger.warning(_msg("⚠️", f"登录后跳转上传页异常: {exc}"))

                if not upload_ready:
                    result = _build_login_result(
                        False,
                        "upload_not_ready",
                        "扫码后未能进入上传页，请重试",
                        account_file,
                        qrcode_info,
                        page.url,
                    )
                else:
                    await context.storage_state(path=account_file)
                    douyin_logger.info(
                        _msg(
                            "💾",
                            f"Cookie 已保存: {account_file}（cookies={len((await context.cookies()))}）",
                        )
                    )
                    cookies = await context.cookies()
                    if not _cookies_have_douyin_session(cookies) and not _storage_state_has_douyin_session(account_file):
                        result = _build_login_result(
                            False,
                            "cookie_invalid",
                            "抖音扫码流程结束，但未获取到有效 session",
                            account_file,
                            qrcode_info,
                            page.url,
                        )
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if remove_qrcode_file(qrcode_path):
                douyin_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                douyin_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            if should_close_context:
                await context.close()
            await browser.close()
        return result


class DouYinBaseUploader(BaseVideoUploader):
    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = account_file
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = headless

    async def validate_base_args(self):
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成抖音登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成抖音登录: {self.account_file}")
        if self.publish_strategy not in {DOUYIN_PUBLISH_STRATEGY_IMMEDIATE, DOUYIN_PUBLISH_STRATEGY_SCHEDULED}:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def set_schedule_time_douyin(self, page, publish_date):
        label_element = page.locator("[class^='radio']:has-text('定时发布')")
        await label_element.click()
        await asyncio.sleep(1)
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M")

        await asyncio.sleep(1)
        await page.locator('.semi-input[placeholder="日期和时间"]').click()
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.type(str(publish_date_hour))
        await page.keyboard.press("Enter")
        await asyncio.sleep(1)

    async def _focus_description_editor(self, page: Page):
        """聚焦作品简介编辑器。发布页常有浮层拦截普通 click，需多策略兜底。"""
        candidates = [
            page.locator('div.zone-container[contenteditable="true"][data-placeholder*="简介"]').first,
            page.locator('div.zone-container[contenteditable="true"]').first,
            page.locator('[data-slate-editor="true"][contenteditable="true"]').first,
            page.locator('div[contenteditable="true"][data-placeholder*="简介"]').first,
        ]
        last_err = None
        for editor in candidates:
            try:
                if await editor.count() == 0:
                    continue
                await editor.wait_for(state="visible", timeout=120000)
                # 1) 普通点击
                try:
                    await editor.click(timeout=8000)
                    return editor
                except Exception as exc:
                    last_err = exc
                    douyin_logger.warning(_msg("⚠️", f"简介框普通点击失败，尝试 force: {exc}"))
                # 2) force 点击（绕过遮挡层）
                try:
                    await editor.click(force=True, timeout=8000)
                    return editor
                except Exception as exc:
                    last_err = exc
                # 3) 直接 focus + 再点一次坐标中心
                try:
                    await editor.focus(timeout=5000)
                    box = await editor.bounding_box()
                    if box:
                        await page.mouse.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + min(24.0, box["height"] / 2),
                        )
                    return editor
                except Exception as exc:
                    last_err = exc
            except Exception as exc:
                last_err = exc
                continue
        raise RuntimeError(f"无法聚焦作品简介编辑框: {last_err}")

    async def fill_title_and_description(self, page: Page, title: str, description: str, tags: list[str] | None = None):
        # 2026-06 抖音发布页 DOM：标题=input[placeholder*=填写作品标题]，描述=div.zone-container[contenteditable]
        # version_2(post/video) 发布页要等视频上传完才渲染表单（实测约 40s），故等待超时给到 120s
        title_input = page.locator('input[placeholder*="填写作品标题"]').first
        await title_input.wait_for(state="visible", timeout=120000)
        await title_input.fill((title or "")[:30])

        # 关掉可能挡住简介框的提示/下拉
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
        except Exception:
            pass

        await self._focus_description_editor(page)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")

        # 先写简介正文（insert_text 整段粘贴，避免逐字过慢）
        body = (description or "").strip()
        if body:
            # 抖音作品描述常见上限约 1000 字，留余量
            await page.keyboard.insert_text(body[:1000])
            douyin_logger.info(_msg("📝", f"已填入简介 {min(len(body), 1000)} 字"))
        else:
            douyin_logger.warning(_msg("⚠️", "简介为空，仅填写话题"))

        for tag in tags or []:
            tag_text = str(tag).strip().lstrip("#")
            if not tag_text:
                continue
            await page.keyboard.insert_text(f" #{tag_text}")
            await page.keyboard.press("Space")
        await page.keyboard.press("Escape")  # 收起话题下拉，避免浮层拦截后续点击
        await asyncio.sleep(0.3)

    async def set_location(self, page: Page, location: str = ""):
        if not location:
            return
        await page.locator('div.semi-select span:has-text("输入地理位置")').click()
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(2000)
        await page.keyboard.type(location)
        await page.wait_for_selector('div[role="listbox"] [role="option"]', timeout=5000)
        await page.locator('div[role="listbox"] [role="option"]').first.click()

    async def handle_product_dialog(self, page: Page, product_title: str):
        await page.wait_for_timeout(2000)
        await page.wait_for_selector('input[placeholder="请输入商品短标题"]', timeout=10000)
        short_title_input = page.locator('input[placeholder="请输入商品短标题"]')
        if not await short_title_input.count():
            douyin_logger.error(_msg("😵", "没找到商品短标题输入框"))
            return False

        product_title = product_title[:10]
        await short_title_input.fill(product_title)
        await page.wait_for_timeout(1000)

        finish_button = page.locator('button:has-text("完成编辑")')
        if "disabled" not in await finish_button.get_attribute("class"):
            await finish_button.click()
            douyin_logger.debug(_msg("🥳", "已点击“完成编辑”按钮"))
            await page.wait_for_selector(".semi-modal-content", state="hidden", timeout=5000)
            return True

        douyin_logger.error(_msg("😵", "“完成编辑”按钮是灰的，小人先把弹窗关掉"))
        cancel_button = page.locator('button:has-text("取消")')
        if await cancel_button.count():
            await cancel_button.click()
        else:
            close_button = page.locator(".semi-modal-close")
            await close_button.click()
        await page.wait_for_selector(".semi-modal-content", state="hidden", timeout=5000)
        return False

    async def set_product_link(self, page: Page, product_link: str, product_title: str):
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector("text=添加标签", timeout=10000)
            dropdown = page.get_by_text("添加标签").locator("..").locator("..").locator("..").locator(".semi-select").first
            if not await dropdown.count():
                douyin_logger.error(_msg("😵", "没找到标签下拉框"))
                return False
            douyin_logger.debug(_msg("🧍", "找到标签下拉框，小人准备选择“购物车”"))
            await dropdown.click()
            await page.wait_for_selector('[role="listbox"]', timeout=5000)
            await page.locator('[role="option"]:has-text("购物车")').click()
            douyin_logger.debug(_msg("🥳", "已经选中“购物车”"))

            await page.wait_for_selector('input[placeholder="粘贴商品链接"]', timeout=5000)
            input_field = page.locator('input[placeholder="粘贴商品链接"]')
            await input_field.fill(product_link)
            douyin_logger.debug(_msg("🔗", f"商品链接已经填好了: {product_link}"))

            add_button = page.locator('span:has-text("添加链接")')
            button_class = await add_button.get_attribute("class")
            if "disable" in button_class:
                douyin_logger.error(_msg("😵", "“添加链接”按钮现在点不了"))
                return False
            await add_button.click()
            douyin_logger.debug(_msg("🥳", "已点击“添加链接”按钮"))

            await page.wait_for_timeout(2000)
            error_modal = page.locator("text=未搜索到对应商品")
            if await error_modal.count():
                confirm_button = page.locator('button:has-text("确定")')
                await confirm_button.click()
                douyin_logger.error(_msg("😢", "这个商品链接无效"))
                return False

            if not await self.handle_product_dialog(page, product_title):
                return False

            douyin_logger.debug(_msg("🥳", "商品链接设置好了"))
            return True
        except Exception as e:
            douyin_logger.error(_msg("😢", f"设置商品链接时出错: {str(e)}"))
            return False

    async def _dismiss_cover_modals(self, page: Page) -> None:
        """强拆封面/内容弹层，避免挡住自主声明与发布按钮。

        无封面弹层时不要按 Escape：新版发布页底部是「发布 / 暂存离开」，
        Esc 会被当成暂存离开，直接跳到创作者首页。
        """
        modal_visible = False
        try:
            modal = page.locator("div.dy-creator-content-modal").first
            modal_visible = bool(await modal.count()) and await modal.is_visible()
        except Exception:
            modal_visible = False
        if not modal_visible:
            return
        try:
            # 优先点确定/完成（确认应用封面）
            for name in ("确定", "完成"):
                btn = page.locator("div.dy-creator-content-modal").get_by_role(
                    "button", name=name, exact=True
                ).first
                if await btn.count() and await btn.is_visible():
                    await btn.click(force=True, timeout=3000)
                    await page.wait_for_timeout(500)
        except Exception:
            pass
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception:
            pass
        try:
            await page.evaluate(
                """() => {
                  document.querySelectorAll(
                    '.dy-creator-content-modal-wrap, .dy-creator-content-modal, .dy-creator-content-portal'
                  ).forEach(e => e.remove());
                }"""
            )
        except Exception:
            pass

    async def set_xingtu_task(self, page: Page, task_id: str) -> None:
        """挂载星图任务：兼容「请选择星图任务」入口与旧版勾选+输入框。"""
        raw = (task_id or "").strip()
        if not raw:
            raise ValueError("星图任务 ID/名称不能为空")

        # 优先纯数字任务 ID（名称搜索在创作者中心经常找不到输入框/联想）
        query = raw
        m_chal = re.search(r"/challenge/(\d{6,})", raw)
        if m_chal:
            query = m_chal.group(1)
        elif re.fullmatch(r"\d{6,}", raw):
            query = raw
        douyin_logger.info(_msg("⭐", f"星图挂载关键词: {query[:80]}"))

        try:
            await self._dismiss_cover_modals(page)
        except Exception:
            pass

        # 懒加载：先滚到页面下部，星图入口常在设置区
        try:
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(600)
            await page.evaluate("() => window.scrollBy(0, -400)")
            await page.wait_for_timeout(300)
        except Exception:
            pass

        opened = False

        # 新版入口：按钮/文案「请选择星图任务」
        for text in ("请选择星图任务", "选择星图任务", "关联星图任务", "添加星图任务"):
            btn = page.get_by_text(text, exact=False).first
            try:
                if await btn.count() and await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click(force=True, timeout=5000)
                    opened = True
                    douyin_logger.info(_msg("⭐", f"已点击星图入口: {text}"))
                    break
            except Exception:
                continue

        # 旧版：勾选「星图任务」开关
        if not opened:
            label = page.get_by_text("星图任务", exact=True).first
            if not await label.count():
                label = page.get_by_text("星图任务", exact=False).first
            await label.wait_for(state="visible", timeout=10000)
            await label.scroll_into_view_if_needed()
            row = page.locator("div,label,span,button").filter(
                has_text=re.compile(r"^星图任务$|星图任务")
            ).first
            toggle = row.locator(
                'input[type="checkbox"], .semi-checkbox, .semi-switch, '
                '.semi-switch-native-control, [role="switch"]'
            ).first
            try:
                if await toggle.count():
                    await toggle.click(force=True, timeout=4000)
                else:
                    await label.click(force=True, timeout=4000)
            except Exception:
                await label.click(force=True)
            opened = True
            douyin_logger.info(_msg("⭐", "已勾选/点击「星图任务」"))

        await asyncio.sleep(1.0)

        # 弹层/下拉内的搜索框（可能挂在 body portal）
        input_box = None
        deadline = time.time() + 15
        while time.time() < deadline and input_box is None:
            candidates = [
                page.get_by_placeholder(
                    re.compile(
                        r"任务\s*id|任务\s*ID|任务id或名称|任务名称|搜索任务|请输入任务|请选择|搜索",
                        re.I,
                    )
                ),
                page.locator(
                    'input[placeholder*="任务"], input[placeholder*="搜索"], '
                    'input[placeholder*="星图"]'
                ),
                page.locator(
                    ".semi-modal input, .semi-portal input, .semi-select-option-list input, "
                    ".semi-select-selection-search input, "
                    '[class*="modal"] input[type="text"], [class*="popover"] input, '
                    '[class*="dropdown"] input'
                ),
                page.locator('[role="dialog"] input, [role="listbox"] input, [role="combobox"]'),
            ]
            for loc in candidates:
                try:
                    n = await loc.count()
                except Exception:
                    continue
                for i in range(min(n, 8)):
                    el = loc.nth(i)
                    try:
                        if await el.is_visible():
                            # 排除售价等无关框
                            ph = (await el.get_attribute("placeholder")) or ""
                            if "售价" in ph or "价格" in ph:
                                continue
                            input_box = el
                            break
                    except Exception:
                        continue
                if input_box is not None:
                    break
            if input_box is None:
                await asyncio.sleep(0.4)

        if input_box is None:
            # 再点一次「请选择星图任务」后重试一轮短等待
            retry_btn = page.get_by_text("请选择星图任务", exact=False).first
            try:
                if await retry_btn.count() and await retry_btn.is_visible():
                    await retry_btn.click(force=True)
                    await asyncio.sleep(0.8)
                    input_box = page.locator(
                        '.semi-modal input:visible, .semi-portal input:visible, '
                        'input[placeholder*="任务"]:visible, input[placeholder*="搜索"]:visible'
                    ).first
                    if not await input_box.count():
                        input_box = None
                    elif not await input_box.is_visible():
                        input_box = None
            except Exception:
                input_box = None

        if input_box is None:
            raise TimeoutError(
                "未找到星图任务搜索/输入框。请确认创作者账号已开通星图，"
                "发布页能看到「请选择星图任务」或「星图任务」开关，并尽量用星图任务ID发布"
            )

        # 整段填入并主动触发 input，避免 Semi 搜索框不联想
        await input_box.click(force=True)
        try:
            await input_box.evaluate(
                """(el, v) => {
                  const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                  ).set;
                  setter.call(el, v);
                  el.dispatchEvent(new Event('input', { bubbles: true }));
                  el.dispatchEvent(new Event('change', { bubbles: true }));
                  el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Enter' }));
                }""",
                query,
            )
        except Exception:
            try:
                await input_box.fill(query, timeout=8000)
            except Exception:
                await page.keyboard.insert_text(query)
        douyin_logger.info(_msg("⭐", f"已填入星图搜索词（{len(query)} 字符）"))
        # 等搜索结果渲染（接口慢时不再死等；最多 1s+5s 轮询）
        # 命中后立即跳出进入 radio card 匹配；未命中也不阻塞，_select_xingtu_via_radio_card 内部
        # 仍会按弹窗内任务卡二次匹配（方案C 兜底）
        await asyncio.sleep(1.0)
        search_hit = False
        for _ in range(10):
            hit = await page.evaluate(
                """(qid) => {
                  const t = document.body ? document.body.innerText : '';
                  return t.includes(qid) || t.includes(qid.slice(-8));
                }""",
                query,
            )
            if hit:
                search_hit = True
                break
            await asyncio.sleep(0.5)
        douyin_logger.info(
            _msg("⭐", f"星图搜索词检测完成 hit={search_hit}")
        )

        clicked = await self._select_xingtu_result(page, query)
        if not clicked:
            douyin_logger.warning(_msg("⚠️", "未勾选到星图任务行，将导出 DOM 便于对照"))
            await self._dump_xingtu_debug(page, query)
            await self._dismiss_xingtu_overlays(page)
            raise TimeoutError(
                f"星图任务未勾选成功（ID={query}）。"
                "请查看 logs/xingtu_debug_*.html / .png"
            )

        # 必须看到 radio 选中态再点确定（JS 空点会关弹窗但挂载失败）
        if not await self._xingtu_radio_is_checked(page):
            douyin_logger.warning(_msg("⚠️", "点击后未见 semi-radio-checked，再点一次"))
            await self._select_xingtu_via_radio_card(page, query)
            if not await self._xingtu_radio_is_checked(page):
                await self._dump_xingtu_debug(page, query)
                await self._dismiss_xingtu_overlays(page)
                raise TimeoutError(
                    f"星图 radio 未真正选中（ID={query}），已中止以免空点确定"
                )

        await asyncio.sleep(0.4)
        confirmed = await self._confirm_xingtu_selection(page)
        if not confirmed:
            await self._dump_xingtu_debug(page, query)
            await self._dismiss_xingtu_overlays(page)
            raise TimeoutError("已勾选星图任务，但未找到可用的「确定」按钮（semi-modal-footer）")
        douyin_logger.info(_msg("🥳", "已点击星图弹窗「确定」"))
        await asyncio.sleep(0.8)

        # 硬校验：入口不能还是「请选择星图任务」
        if await self._xingtu_still_unmounted(page):
            await self._dump_xingtu_debug(page, query)
            await self._dismiss_xingtu_overlays(page)
            raise TimeoutError(
                f"星图挂载未生效：入口仍显示「请选择星图任务」（ID={query}）"
            )
        douyin_logger.info(_msg("🥳", "星图入口已变更，挂载成功"))

    async def _xingtu_modal(self, page: Page):
        """仅定位「选择星图任务」弹窗（避免命中页面其它 radio / card）。"""
        modal = page.locator(".semi-modal").filter(
            has=page.locator(".semi-modal-title", has_text="选择星图任务")
        ).first
        if await modal.count():
            return modal
        modal = page.locator(".semi-modal").filter(has_text="选择星图任务").first
        if await modal.count():
            return modal
        return page.locator("#dialog-0.semi-modal, .semi-modal-small").first

    async def _select_xingtu_result(self, page: Page, query: str) -> bool:
        """双方案勾选星图任务（一律限定在星图弹窗内）：

        A. card-container + label.semi-radio（当前主路径）
        B. 下拉联想项（旧版兜底）
        """
        if await self._select_xingtu_via_radio_card(page, query):
            return True
        douyin_logger.info(_msg("🧭", "未命中星图 radio 卡片，尝试旧版联想项方案"))
        if await self._select_xingtu_via_suggest_option(page, query):
            return True
        return False

    async def _modal_radio_really_checked(self, modal) -> bool:
        """弹窗内是否真实选中：checked class + inner-checked（选中后会出现 SVG）。"""
        try:
            label_ok = await modal.locator("label.semi-radio-checked").count() > 0
            inner_ok = await modal.locator(".semi-radio-inner-checked").count() > 0
            svg_ok = await modal.locator("svg.semi-icons-radio").count() > 0
            return bool(label_ok and (inner_ok or svg_ok))
        except Exception:
            return False

    async def _click_xingtu_card_radio(self, page: Page, modal, card) -> str | None:
        """在星图弹窗内点选任务卡；只认弹窗内的真实选中态。"""
        await card.scroll_into_view_if_needed()
        await page.wait_for_timeout(200)

        # 1) 弹窗内原生 label.click()（比 page.mouse 坐标更稳，且不误点页外节点）
        try:
            ok = await card.evaluate(
                """(card) => {
                  const label = card.querySelector('label.semi-radio');
                  const display = card.querySelector('.semi-radio-inner-display');
                  const input = card.querySelector('input[type="radio"]');
                  if (display) display.click();
                  if (label) label.click();
                  else if (input) input.click();
                  const checked = !!(label && label.classList.contains('semi-radio-checked'));
                  const inner = !!card.querySelector('.semi-radio-inner-checked');
                  const svg = !!card.querySelector('svg.semi-icons-radio');
                  return checked && (inner || svg);
                }"""
            )
            await page.wait_for_timeout(350)
            if ok or await self._modal_radio_really_checked(modal):
                return "label.click()"
        except Exception as exc:
            douyin_logger.debug(_msg("🔍", f"label.click 失败: {exc}"))

        # 2) Playwright 点可见圆点 / label（限定在 card 内）
        targets = [
            ("inner-display", card.locator(".semi-radio-inner-display").first),
            ("label.semi-radio", card.locator("label.semi-radio").first),
            ("radio-container", card.locator('[class*="radio-container"]').first),
            ("info-container", card.locator('[class*="info-container"]').first),
        ]
        for via, loc in targets:
            try:
                if not await loc.count() or not await loc.is_visible():
                    continue
                await loc.click(timeout=4000)
                await page.wait_for_timeout(400)
                if await self._modal_radio_really_checked(modal):
                    return via
            except Exception:
                continue

        # 3) 键盘：聚焦 radio 后空格
        try:
            radio = card.locator('input[type="radio"]').first
            if await radio.count():
                await radio.focus()
                await page.keyboard.press("Space")
                await page.wait_for_timeout(400)
                if await self._modal_radio_really_checked(modal):
                    return "keyboard-space"
        except Exception:
            pass
        return None

    async def _select_xingtu_via_radio_card(self, page: Page, query: str) -> bool:
        """方案 A：只在「选择星图任务」弹窗内操作（修复页外 radio 误判）。"""
        tail = query[-8:] if len(query) >= 8 else query
        modal = await self._xingtu_modal(page)
        try:
            await modal.wait_for(state="visible", timeout=10000)
        except Exception:
            douyin_logger.warning(_msg("⚠️", "[方案A-radio] 未找到「选择星图任务」弹窗"))
            return False

        cards = modal.locator('[class*="card-container"]')
        # 等弹窗内出现任务卡片（最多 20*0.4s=8s；典型 1-2s 内即可命中）
        for _ in range(20):
            try:
                if await cards.count() > 0:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.4)

        n = await cards.count()
        douyin_logger.info(_msg("⭐", f"[方案A-radio] 弹窗内任务卡片数={n}"))
        if n <= 0:
            return False

        # 优先正文含 ID 的卡；否则首张（搜索结果通常只剩目标任务）
        card = cards.filter(has_text=query).first
        if not await card.count():
            card = cards.filter(has_text=tail).first
        if not await card.count():
            card = cards.first

        via = await self._click_xingtu_card_radio(page, modal, card)
        if via and await self._modal_radio_really_checked(modal):
            douyin_logger.info(_msg("🥳", f"[方案A-radio] 弹窗内真实选中（via={via}）"))
            return True

        # 再扫一遍弹窗内每张卡
        for i in range(n):
            via = await self._click_xingtu_card_radio(page, modal, cards.nth(i))
            if via and await self._modal_radio_really_checked(modal):
                douyin_logger.info(
                    _msg("🥳", f"[方案A-radio] 弹窗内真实选中（第{i + 1}张, via={via}）")
                )
                return True

        douyin_logger.warning(
            _msg(
                "⚠️",
                "[方案A-radio] 弹窗内仍无 semi-radio-checked + inner-checked（圆点未亮）",
            )
        )
        return False

    async def _select_xingtu_via_suggest_option(self, page: Page, query: str) -> bool:
        """方案 B：旧版下拉联想项（semi-select-option / role=option 等）。"""
        tail = query[-8:] if len(query) >= 8 else query
        option_roots = page.locator(
            ".semi-select-option, .semi-list-item, [class*='option'], [class*='suggest'], "
            "[role='option'], .semi-cascader-option, [class*='task-item'], [class*='TaskItem']"
        )
        option = option_roots.filter(has_text=query).first
        if not await option.count():
            option = option_roots.filter(has_text=tail).first

        # 等下拉项可见（最多 8*0.5s=4s）
        for _ in range(8):
            try:
                if await option.count() and await option.is_visible():
                    break
            except Exception:
                pass
            any_opt = option_roots.filter(has_text=re.compile(r"\d{6,}|星图|任务|推广")).first
            try:
                if await any_opt.count() and await any_opt.is_visible():
                    option = any_opt
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)

        try:
            if await option.count() and await option.is_visible():
                await option.click(force=True, timeout=5000)
                douyin_logger.info(_msg("🥳", f"[方案B-联想] 已点击联想项（含 {tail}）"))
                return True
        except Exception as exc:
            douyin_logger.warning(_msg("⚠️", f"[方案B-联想] 点击失败: {exc}"))

        for cand in (
            option_roots.filter(has_text=query).first,
            option_roots.filter(has_text=tail).first,
            option_roots.filter(has_text=re.compile(r"\d{10,}")).first,
        ):
            try:
                if await cand.count() and await cand.is_visible():
                    await cand.click(force=True, timeout=4000)
                    douyin_logger.info(_msg("🥳", "[方案B-联想] 已点击匹配项"))
                    return True
            except Exception:
                continue

        # 最后：键盘选中（部分下拉只响应方向键）
        try:
            await page.keyboard.press("ArrowDown")
            await asyncio.sleep(0.2)
            await page.keyboard.press("Enter")
            # 无法可靠判断是否生效，仅当页面上已有勾选/入口变化时算成功
            await asyncio.sleep(0.4)
            still = page.get_by_text("请选择星图任务", exact=False).first
            if await still.count() and await still.is_visible():
                douyin_logger.warning(_msg("⚠️", "[方案B-联想] 回车后入口仍为空，视为未选中"))
                return False
            douyin_logger.info(_msg("🥳", "[方案B-联想] 回车后入口文案已变化"))
            return True
        except Exception:
            return False

    async def _dump_xingtu_debug(self, page: Page, query: str) -> None:
        """导出星图弹层 HTML/截图，便于对照真实 DOM。"""
        try:
            from pathlib import Path
            from datetime import datetime

            out_dir = Path(__file__).resolve().parents[4] / "backend" / "logs"
            # social-auto-upload 可能在独立 cwd；回退到 cwd/logs
            if not out_dir.is_dir():
                out_dir = Path.cwd() / "logs"
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            html_path = out_dir / f"xingtu_debug_{stamp}.html"
            png_path = out_dir / f"xingtu_debug_{stamp}.png"
            snippet = await page.evaluate(
                """(qid) => {
                  const modals = [...document.querySelectorAll('.semi-modal')];
                  const pick = modals.find(m => (m.innerText || '').includes('选择星图任务'))
                    || document.querySelector('#dialog-0')
                    || modals[0];
                  const html = pick
                    ? pick.outerHTML.slice(0, 400000)
                    : '';
                  const checked = !!(pick && pick.querySelector('label.semi-radio-checked'));
                  const inner = !!(pick && pick.querySelector('.semi-radio-inner-checked'));
                  const svg = !!(pick && pick.querySelector('svg.semi-icons-radio'));
                  return `<!-- query=${qid} url=${location.href} modal_checked=${checked} inner=${inner} svg=${svg} -->\\n` + html;
                }""",
                query,
            )
            html_path.write_text(snippet or "", encoding="utf-8", errors="ignore")
            try:
                await page.screenshot(path=str(png_path), full_page=False)
            except Exception:
                pass
            douyin_logger.warning(
                _msg("📎", f"星图调试已导出: {html_path.name} / {png_path.name}（目录 {out_dir}）")
            )
        except Exception as exc:
            douyin_logger.warning(_msg("⚠️", f"导出星图调试失败: {exc}"))

    async def _xingtu_radio_is_checked(self, page: Page) -> bool:
        """仅检查「选择星图任务」弹窗内的真实选中态（勿扫全页其它 radio）。"""
        try:
            modal = await self._xingtu_modal(page)
            if not await modal.count() or not await modal.is_visible():
                return False
            return await self._modal_radio_really_checked(modal)
        except Exception:
            return False

    async def _xingtu_still_unmounted(self, page: Page) -> bool:
        """发布页星图入口是否仍为未选择状态（排除弹窗标题「选择星图任务」）。"""
        try:
            # 弹窗还开着时不算「已挂载失败」的最终态，先以入口文案为准
            tip = page.locator("body").get_by_text("请选择星图任务", exact=True).first
            if await tip.count() and await tip.is_visible():
                return True
            # 宽松：发布页扩展信息区仍显示请选择
            tip2 = page.get_by_text("请选择星图任务", exact=False).first
            if not await tip2.count() or not await tip2.is_visible():
                return False
            # 若点中的是弹窗标题「选择星图任务」，不算未挂载入口
            txt = (await tip2.inner_text() or "").strip()
            return txt.startswith("请选择") or "请选择星图任务" in txt
        except Exception:
            return False

    async def _confirm_xingtu_selection(self, page: Page) -> bool:
        """只点星图弹窗 footer 的 primary「确定」。"""
        modal = await self._xingtu_modal(page)
        try:
            if not await modal.count() or not await modal.is_visible():
                return False
            # 确定前再确认弹窗内已选中
            if not await self._modal_radio_really_checked(modal):
                douyin_logger.warning(_msg("⚠️", "弹窗内任务未选中，拒绝点击确定"))
                return False
            btn = modal.locator("div.semi-modal-footer button.semi-button-primary").filter(
                has_text=re.compile(r"^确定$")
            ).first
            if not await btn.count():
                btn = modal.get_by_role("button", name="确定", exact=True).first
            if not await btn.count() or not await btn.is_visible():
                return False
            cls = (await btn.get_attribute("class")) or ""
            if "semi-button-disabled" in cls:
                douyin_logger.warning(_msg("⚠️", "星图「确定」按钮禁用"))
                return False
            await btn.click(timeout=5000)
            await page.wait_for_timeout(600)
            return True
        except Exception as exc:
            douyin_logger.warning(_msg("⚠️", f"点击星图确定失败: {exc}"))
            return False

    async def _dismiss_xingtu_overlays(self, page: Page) -> None:
        """关闭星图选择残留层：只点取消/Esc，绝不点确定。

        无可见 semi-modal 时不要 Esc，更不要点击左上角坐标：
        新版发布页 Esc / 点到 Logo 会触发「暂存离开」跳转创作者首页。
        """
        modal_visible = False
        try:
            modal = page.locator(".semi-modal-wrapper, div.semi-modal").first
            modal_visible = bool(await modal.count()) and await modal.is_visible()
        except Exception:
            modal_visible = False
        if not modal_visible:
            return
        try:
            cancel = page.locator("div.semi-modal-footer button.semi-button-tertiary").filter(
                has_text=re.compile(r"^取消$")
            ).first
            if await cancel.count() and await cancel.is_visible():
                await cancel.click(force=True, timeout=3000)
                await page.wait_for_timeout(300)
        except Exception:
            pass
        for _ in range(2):
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(250)
            except Exception:
                break

    async def set_self_declaration(
        self, page: Page, declaration: str = "内容由AI生成", *, must_succeed: bool = True
    ) -> None:
        """抖音「自主声明」：打开弹窗 → 选指定类型 → 确定。

        参数 must_succeed:
          - True  (默认，上传完后调用/发布循环补调用)：未选声明抖音必拦截发布按钮，
            任何异常都直接 raise RuntimeError 中断发布
          - False (上传进行中/并行调用)：此时发布页 DCFF 底部可能还在懒加载渲染，
            失败记 warning 即可，上传完后会再补一次（must_succeed=True）

        标题兼容两版：
          - 老版：「对作品内容添加声明」
          - 2026-08-08 新版：「请选择声明类型（单选）」（用户截图 header: selectorHeader-jxI9fI）
        """
        def _fail(msg: str) -> None:
            if must_succeed:
                raise RuntimeError(msg)
            douyin_logger.warning(_msg("🧾", f"[并行尝试] 自主声明设置失败（上传完成后会再补一次）: {msg}"))

        # ================= 前置判定：已选声明则直接 return，避免无意义 raise =================
        # 优先调用 pending 检查（与调用方口径一致）
        if not await _douyin_has_self_declaration_pending(page):
            # 没显示「请选择自主声明」→ 要么已经选了，要么页面没有该配置。
            # 尝试读取当前显示值打印到日志，便于上层确认。
            try:
                cur_val = await page.evaluate("""() => {
                    const list = document.querySelectorAll('section[class*="wrapper-"] div[class*="selectBox-"] div[class*="selectText-"], div[class*="selectBox-"] div[class*="selectText-"], div[class*="select-box-"]');
                    for (const el of list) {
                        const t = (el.textContent || '').trim();
                        if (t && t.length < 40 && !t.includes('请选择')) return t;
                    }
                    return null;
                }""")
            except Exception:
                cur_val = None
            if cur_val:
                douyin_logger.info(
                    _msg("🧾", f"自主声明已设置「{cur_val}」，跳过本次 set_self_declaration（must_succeed={must_succeed}）")
                )
            else:
                douyin_logger.info(
                    _msg("🧾", f"页面未显示待选声明入口，跳过设置（must_succeed={must_succeed}）")
                )
            return
        # ===================================================================================

        # 文案别名：抖音偶发空格/「为」写法差异 + 用户截图新选项「内容为个人观点或见解」
        declaration_aliases = [
            declaration,
            "内容由AI生成",
            "内容由 AI 生成",
            "内容为AI生成",
            "内容为 AI 生成",
            "内容为个人观点或见解",
            "内容为个人观点或见解（非事实）",
        ]
        # 封面弹层未关时会拦截点击（pointer-events），先清掉
        try:
            await self._dismiss_cover_modals(page)
        except Exception:
            pass
        # 滚到底部让 DCFF 底部的声明区渲染（2026-08 该区域是滚动懒加载）
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
            await page.wait_for_timeout(200)
        except Exception:
            pass

        # 打开入口（7 级选择器兜底，含 2026-08-08 驼峰 selectBox- + DCFF 区域锁定）
        opened = await _douyin_set_self_declaration_entry(page)
        if not opened:
            # 理论上前面 pending=true 保证了入口存在；但为了绝对安全，这里再判一次
            _fail("未定位到自主声明入口（详情查看 DEBUG 日志 [声明入口-ALL] 7 级未命中候选 class）")
            return

        # 定位弹窗：兼容两版标题（只要有一版出现就算打开成功）
        dialog = None
        for title in ("请选择声明类型（单选）", "对作品内容添加声明"):
            try:
                d = page.locator(".semi-modal-content").filter(has_text=title).first
                await d.wait_for(state="visible", timeout=4000)
                dialog = d
                douyin_logger.info(_msg("🧾", f"自主声明弹窗已打开（标题: {title}）"))
                break
            except Exception:
                continue
        if dialog is None:
            # 标题都不匹配，但确实有打开过的 semi-modal，兜底用第一个可见弹窗
            any_dialog = page.locator(".semi-modal-content").first
            if await any_dialog.count() and await any_dialog.is_visible():
                dialog = any_dialog
                douyin_logger.warning(_msg("🧾", "自主声明弹窗标题未命中，退回第一个可见弹窗"))
            else:
                _fail("点击自主声明入口后未打开弹窗（两版标题均未出现）")
                return

        # 单选项：优先点可交互的 .semi-radio 外层，避免 pointer-events:none 的 inner/addon 卡超时
        clicked = False
        for text in declaration_aliases:
            try:
                option = dialog.locator(".semi-radio").filter(has_text=text).first
                if await option.count():
                    await option.click(timeout=6000, force=True)
                    declaration = text
                    clicked = True
                    break
            except Exception:
                pass
            try:
                label = dialog.get_by_text(text, exact=True).first
                if await label.count():
                    await label.click(timeout=6000, force=True)
                    declaration = text
                    clicked = True
                    break
            except Exception:
                pass
        if not clicked:
            # 没点到选项前先关弹窗，不然遮罩影响后续操作
            try:
                await dialog.get_by_text("取消", exact=True).first.click(force=True)
            except Exception:
                pass
            _fail(f"未找到自主声明选项（已尝试 {declaration_aliases}），请检查页面是否有新增声明类型")
            return

        # 点确定 → 等待弹窗关闭
        ok_btn = dialog.get_by_role("button", name="确定")
        if not await ok_btn.count():
            # 新版弹窗 footer 的确定按钮可能不是 role=button 精确，用文字兜底
            ok_btn = dialog.get_by_text("确定", exact=True).first
        await ok_btn.click(timeout=6000, force=True)
        try:
            await dialog.wait_for(state="hidden", timeout=6000)
        except Exception:
            # 个别场景弹窗关了就不拦发布；失败的话 pending 检查会在上层循环重试
            douyin_logger.warning(_msg("🧾", "自主声明确定后未观察到弹窗关闭，继续发布流程"))
        douyin_logger.info(_msg("🧾", f"自主声明已选择「{declaration}」（must_succeed={must_succeed}）"))

    async def select_bgm(self, page: Page, bgm_name: str) -> bool:
        """为图文发布选择 BGM：可选增强功能，搜索无结果或异常均跳过不中断发布。"""
        try:
            # 点击「选择音乐」按钮
            music_entry = page.locator('text="选择音乐"').nth(1)
            if not await music_entry.count():
                music_entry = page.locator('text="选择音乐"').first
            await music_entry.wait_for(state="visible", timeout=10000)
            await music_entry.click()

            # 等待侧边栏出现并搜索
            sidesheet = page.locator(".semi-sidesheet-content").first
            await sidesheet.wait_for(state="visible", timeout=8000)
            search_input = sidesheet.locator('input.semi-input[placeholder="搜索音乐"]').first
            await search_input.wait_for(state="visible", timeout=5000)
            await search_input.fill(bgm_name)
            await search_input.press("Enter")

            # 等待搜索结果
            await asyncio.sleep(2)
            first_card = sidesheet.locator(".card-container-tmocjc").first
            try:
                await first_card.wait_for(state="visible", timeout=8000)
            except Exception:
                douyin_logger.warning(_msg("🎵", f"音乐「{bgm_name}」搜索结果为空，小人跳过"))
                await self._close_music_sidesheet(page)
                return False

            # 打印找到的音乐名称
            try:
                song_name_el = first_card.locator(".song-name-oRge4d").first
                if await song_name_el.count():
                    song_name = await song_name_el.inner_text()
                    douyin_logger.info(_msg("🎵", f"小人找到了: {song_name}"))
            except Exception:
                pass

            # JS 点击「使用」（按钮 visibility:hidden，普通 click 无效）
            apply_btn = first_card.locator(".apply-btn-LUPP0D").first
            await apply_btn.evaluate("el => el.click()")
            douyin_logger.info(_msg("🥳", f"BGM「{bgm_name}」已应用"))

            # 等待侧边栏关闭，超时则手动关闭
            try:
                await sidesheet.wait_for(state="hidden", timeout=5000)
            except Exception:
                await self._close_music_sidesheet(page)

            return True
        except Exception as exc:
            douyin_logger.warning(_msg("🎵", f"添加 BGM 时出错，跳过该步骤继续发布：{exc}"))
            try:
                await self._close_music_sidesheet(page)
            except Exception:
                pass
            return False

    async def _close_music_sidesheet(self, page: Page) -> None:
        try:
            close_btn = page.locator(".semi-sidesheet-close").first
            if await close_btn.count() and await close_btn.is_visible():
                await close_btn.click()
                await asyncio.sleep(1)
        except Exception:
            pass


class DouYinVideo(DouYinBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        thumbnail_landscape_path=None,
        productLink="",
        productTitle="",
        thumbnail_portrait_path=None,
        desc: str | None = None,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        xingtu_task_id: str | None = None,
        collection_name: str | None = None,
        collection_id: str | None = None,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.title = title
        self.file_path = file_path
        self.tags = tags
        self.thumbnail_landscape_path = thumbnail_landscape_path
        self.thumbnail_portrait_path = thumbnail_portrait_path
        self.productLink = productLink
        self.productTitle = productTitle
        self.desc = desc or ""
        self.xingtu_task_id = (xingtu_task_id or "").strip()
        # 合集配置：将视频发布到指定的抖音合集中
        self.collection_name = (collection_name or "").strip()
        self.collection_id = (collection_id or "").strip()

    async def _select_douyin_collection(self, page: Page) -> bool:
        """
        选择抖音合集。
        根据你提供的三张图：
        - 选择前：semi-select-collection 是 .semi-select，内含「请选择合集」占位文本
        - 选择中：弹出 semi-popover，其中是 semi-select-option-list + collection-option
        - 选择后：semi-select-collection 内部变为 .selected-item-title / .selected-item-extra-text

        返回 True 表示成功选择或保持已选；False 表示不可用。
        """
        if not self.collection_name:
            return False

        # 1) 定位合集下拉框
        try:
            collection_select = page.locator(
                "div.semi-select.semi-select-collection.semi-select-single"
            ).first
            await collection_select.wait_for(state="visible", timeout=8000)
        except Exception as exc:
            douyin_logger.warning(
                _msg("⚠️", f"未找到合集下拉框（可能账号/页面未支持合集）: {exc}")
            )
            return False

        # 2) 检查是否已经是目标合集（避免重复点击）
        try:
            current_text = (await collection_select.inner_text(timeout=2000)) or ""
            if self.collection_name in current_text and "请选择合集" not in current_text:
                douyin_logger.info(
                    _msg("✅", f"合集已选: {self.collection_name}（跳过）")
                )
                return True
        except Exception:
            pass

        # 3) 点击下拉框打开选择面板
        try:
            await collection_select.click(force=True)
            await page.wait_for_timeout(600)
        except Exception as exc:
            douyin_logger.warning(_msg("⚠️", f"打开合集下拉框失败: {exc}"))
            return False

        # 4) 等待下拉面板出现（semi-popover 包裹 semi-select-option-list）
        try:
            await page.wait_for_selector(
                ".semi-popover-content .semi-select-option-list",
                state="visible",
                timeout=8000,
            )
        except Exception as exc:
            douyin_logger.warning(_msg("⚠️", f"合集选项列表未展开: {exc}"))
            return False

        # 5) 在下拉列表中查找目标合集（注意区分已选中的「已选择」伪元素）
        try:
            # 定位 collection-option（合集选项，带 collection-option 类）
            options = page.locator(
                ".semi-popover-content .semi-select-option-list .collection-option"
            )
            count = await options.count()
            douyin_logger.info(
                _msg("🔍", f"合集候选数量: {count}（目标: {self.collection_name}）")
            )

            target_idx = -1
            for i in range(count):
                opt = options.nth(i)
                text = (await opt.inner_text(timeout=2000)) or ""
                # 已选中的项会带 .semi-select-option-selected，跳过
                klass = (await opt.get_attribute("class") or "")
                if "semi-select-option-selected" in klass:
                    continue
                if self.collection_name in text:
                    target_idx = i
                    break

            if target_idx < 0:
                # 候选中没有目标合集 → 抖音未自动创建同名合集
                # 这里不做「新建合集」（抖音创作者中心需到「合集管理」手动创建）
                douyin_logger.warning(
                    _msg(
                        "⚠️",
                        f"未在候选中找到合集「{self.collection_name}」。"
                        "请先在抖音创作者中心→合集管理中创建该合集。",
                    )
                )
                # 关闭面板（点击页面其他位置或 ESC）
                try:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
                except Exception:
                    pass
                return False

            await options.nth(target_idx).click(force=True)
            await page.wait_for_timeout(600)
        except Exception as exc:
            douyin_logger.warning(_msg("⚠️", f"点击合集选项失败: {exc}"))
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return False

        # 6) 校验：semi-select-collection 内部出现 selected-item-title 即视为成功
        try:
            await page.wait_for_selector(
                "div.semi-select.semi-select-collection .selected-item-title",
                state="visible",
                timeout=5000,
            )
            selected_title = await page.locator(
                "div.semi-select.semi-select-collection .selected-item-title"
            ).first.inner_text(timeout=2000)
            douyin_logger.success(
                _msg("🥳", f"已选择合集: {selected_title}")
            )
            return True
        except Exception as exc:
            douyin_logger.warning(
                _msg("⚠️", f"合集选择后未检测到选中态: {exc}")
            )
            return False

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")

        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_landscape_path:
            self.thumbnail_landscape_path = str(self.validate_image_file(self.thumbnail_landscape_path))
        if self.thumbnail_portrait_path:
            self.thumbnail_portrait_path = str(self.validate_image_file(self.thumbnail_portrait_path))

    async def handle_upload_error(self, page):
        douyin_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await _set_douyin_upload_file(page, self.file_path)

    async def handle_auto_video_cover(self, page):
        if await page.get_by_text("请设置封面后再发布").first.is_visible():
            douyin_logger.info(_msg("🧍", "发布前还得先把封面弄好"))
            recommend_cover = page.locator('[class^="recommendCover-"]').first
            if await recommend_cover.count():
                douyin_logger.info(_msg("🏃", "小人去选第一个推荐封面"))
                try:
                    await recommend_cover.click()
                    await asyncio.sleep(1)
                    confirm_text = "是否确认应用此封面？"
                    if await page.get_by_text(confirm_text).first.is_visible():
                        douyin_logger.info(_msg("🪟", f"弹出确认框了: {confirm_text}"))
                        await page.get_by_role("button", name="确定").click()
                        douyin_logger.info(_msg("🥳", "推荐封面已经应用"))
                        await asyncio.sleep(1)
                    douyin_logger.info(_msg("🥳", "封面选择流程完成"))
                    return True
                except Exception as e:
                    douyin_logger.warning(_msg("😵", f"推荐封面没选成功: {e}"))
        return False

    async def _cover_preview_state(self, cover_locator) -> dict:
        """读取封面弹窗预览状态，用于判断自定义图是否真正进入裁剪区。"""
        try:
            return await cover_locator.evaluate(
                """(el) => {
                  const imgs = [...el.querySelectorAll('img')]
                    .map((i) => i.src || '')
                    .filter((s) => s && !s.includes('data:image/svg'));
                  const canvases = el.querySelectorAll('canvas').length;
                  const text = el.innerText || '';
                  return {
                    imgs,
                    canvases,
                    reupload: text.includes('重新上传'),
                    crop: text.includes('自由裁剪') || text.includes('智能裁剪') || text.includes('拖拽'),
                  };
                }"""
            )
        except Exception:
            return {"imgs": [], "canvases": 0, "reupload": False, "crop": False}

    def _cover_preview_ready(self, state: dict, before: dict | None = None) -> bool:
        if not state:
            return False
        if state.get("reupload") or state.get("crop") or int(state.get("canvases") or 0) > 0:
            return True
        imgs = state.get("imgs") or []
        if any(s.startswith("blob:") or s.startswith("http") for s in imgs):
            if before is None:
                return True
            return imgs != (before.get("imgs") or [])
        return False

    async def _upload_cover_via_file_chooser(self, page: Page, cover_locator, thumb_path: str) -> bool:
        """优先用 file chooser：点击可见上传区，避免塞错 AI 参考图 hidden input。"""
        click_targets = [
            cover_locator.get_by_text("点击上传", exact=True).first,
            cover_locator.get_by_text("上传图片", exact=True).first,
            cover_locator.locator(".semi-upload-drag").last,
            cover_locator.locator(".semi-upload").last,
            cover_locator.get_by_text("上传封面", exact=True).first,
        ]
        for idx, target in enumerate(click_targets):
            try:
                if not await target.count() or not await target.is_visible():
                    continue
                async with page.expect_file_chooser(timeout=6000) as fc_info:
                    await target.click(force=True)
                chooser = await fc_info.value
                await chooser.set_files(thumb_path)
                douyin_logger.info(_msg("🔍", f"封面 fileChooser 上传成功（target#{idx}）"))
                return True
            except Exception as exc:
                douyin_logger.debug(_msg("🔍", f"fileChooser target#{idx} 失败: {exc}"))
        return False

    async def _upload_cover_via_inputs(self, cover_locator, thumb_path: str) -> bool:
        """回退：按 input 索引逐个试，封面位优先，直到预览就绪。"""
        inputs = cover_locator.locator("input.semi-upload-hidden-input, input[type='file']")
        n = await inputs.count()
        douyin_logger.info(_msg("🔍", f"封面弹窗 file input 数量={n}"))
        if n <= 0:
            return False
        order = [i for i in (2, 3, 1, 0) if i < n]
        for i in range(n):
            if i not in order:
                order.append(i)
        for i in order:
            before = await self._cover_preview_state(cover_locator)
            try:
                await inputs.nth(i).set_input_files(thumb_path)
            except Exception as exc:
                douyin_logger.debug(_msg("🔍", f"input#{i} set_input_files 失败: {exc}"))
                continue
            ok = False
            for _ in range(20):
                state = await self._cover_preview_state(cover_locator)
                if self._cover_preview_ready(state, before):
                    ok = True
                    break
                await asyncio.sleep(0.4)
            if ok:
                douyin_logger.info(_msg("🔍", f"封面 input#{i} 上传后预览已就绪"))
                return True
            douyin_logger.warning(_msg("⚠️", f"封面 input#{i} 未出现预览，尝试下一个"))
        return False

    async def _verify_cover_on_publish_page(self, page: Page) -> bool:
        """发布页「选择封面」附近是否已有缩略图。"""
        try:
            tip = page.get_by_text("请设置封面后再发布").first
            if await tip.count():
                try:
                    if await tip.is_visible():
                        return False
                except Exception:
                    pass
            entry = page.get_by_text("选择封面", exact=True).first
            if not await entry.count():
                return False
            box = entry.locator("xpath=ancestor::div[4]")
            imgs = box.locator("img")
            if await imgs.count() == 0:
                box = page.locator("div").filter(has_text="选择封面").first
                imgs = box.locator("img")
            for i in range(min(await imgs.count(), 6)):
                src = await imgs.nth(i).get_attribute("src") or ""
                if src.startswith("http") or src.startswith("blob:") or src.startswith("//"):
                    return True
        except Exception:
            return False
        return False

    async def set_thumbnail(self, page: Page):
        if not self.thumbnail_landscape_path and not self.thumbnail_portrait_path:
            return

        douyin_logger.info(_msg("🏃", "小人正在设置视频封面"))
        try:
            await self._set_thumbnail_inner(page)
        except Exception as exc:
            # 封面失败不应整单失败：后续可用推荐封面 / 无封面继续发布
            douyin_logger.warning(_msg("⚠️", f"自定义封面设置失败，跳过继续发布: {exc}"))
            try:
                await self._dismiss_cover_modals(page)
            except Exception:
                pass

    def _derive_landscape_4x3(self, src_path: str) -> str:
        """从竖图中心裁出 4:3 横封面；无 Pillow 时回退原图交给抖音裁切。"""
        src = Path(src_path)
        if not src.is_file():
            return src_path
        try:
            from PIL import Image
        except ImportError:
            douyin_logger.info(_msg("🧭", "未安装 Pillow，横封面沿用原图（由抖音裁切）"))
            return src_path
        try:
            with Image.open(src) as im0:
                im = im0.convert("RGB")
                w, h = im.size
                if w <= 0 or h <= 0:
                    return src_path
                target = 4 / 3
                cur = w / h
                if abs(cur - target) < 0.03:
                    return src_path
                if cur > target:
                    new_w = max(1, int(h * target))
                    left = (w - new_w) // 2
                    im = im.crop((left, 0, left + new_w, h))
                else:
                    new_h = max(1, int(w / target))
                    top = (h - new_h) // 2
                    im = im.crop((0, top, w, top + new_h))
                out = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp") / (
                    f"douyin_cover_4x3_{os.getpid()}_{int(time.time())}.jpg"
                )
                im.save(out, quality=92)
                douyin_logger.info(_msg("🧭", f"已生成横封面 4:3: {out}"))
                return str(out)
        except Exception as exc:
            douyin_logger.warning(_msg("⚠️", f"生成横封面失败，沿用原图: {exc}"))
            return src_path

    async def _switch_cover_orientation(self, cover_locator, orientation: str) -> None:
        """切换竖封面 3:4 / 横封面 4:3。"""
        labels = (
            ("设置竖封面", "竖封面", "竖封面3:4", "3:4")
            if orientation == "portrait"
            else ("设置横封面", "横封面", "横封面4:3", "4:3")
        )
        for name in labels:
            try:
                tab = cover_locator.get_by_text(name, exact=True).first
                if await tab.count() and await tab.is_visible():
                    await tab.click(force=True, timeout=3000)
                    await asyncio.sleep(0.5)
                    douyin_logger.info(_msg("🧭", f"已切换到「{name}」"))
                    return
            except Exception:
                continue
        pat = re.compile(r"竖封面|3\s*:\s*4") if orientation == "portrait" else re.compile(
            r"横封面|4\s*:\s*3"
        )
        try:
            tab = cover_locator.get_by_text(pat).first
            if await tab.count() and await tab.is_visible():
                await tab.click(force=True, timeout=3000)
                await asyncio.sleep(0.5)
                douyin_logger.info(
                    _msg(
                        "🧭",
                        f"已切换到{'竖' if orientation == 'portrait' else '横'}封面（模糊匹配）",
                    )
                )
        except Exception:
            pass

    async def _switch_cover_upload_tab(self, cover_locator) -> None:
        for tab_name in ("上传封面", "本地上传"):
            try:
                tab = cover_locator.get_by_text(tab_name, exact=True).first
                if await tab.count() and await tab.is_visible():
                    await tab.click(force=True)
                    await asyncio.sleep(0.6)
                    douyin_logger.info(_msg("🧭", f"已切换封面页签「{tab_name}」"))
                    return
            except Exception:
                continue

    async def _inject_cover_file(self, page: Page, cover_locator, thumb_path: str) -> bool:
        before = await self._cover_preview_state(cover_locator)
        uploaded = await self._upload_cover_via_inputs(cover_locator, thumb_path)
        if uploaded:
            douyin_logger.info(_msg("🔍", "封面已通过 file input 注入（无系统文件框）"))
            return True
        uploaded = await self._upload_cover_via_file_chooser(page, cover_locator, thumb_path)
        if not uploaded:
            return False
        for _ in range(30):
            if self._cover_preview_ready(await self._cover_preview_state(cover_locator), before):
                return True
            await asyncio.sleep(0.4)
        return False

    async def _click_cover_finish(self, page: Page, cover_locator) -> None:
        finish_btn = cover_locator.get_by_role("button", name="完成", exact=True).first
        for _ in range(40):
            try:
                if await finish_btn.count() and await finish_btn.is_enabled():
                    break
            except Exception:
                pass
            await page.wait_for_timeout(500)
        await finish_btn.click(force=True, timeout=8000)
        douyin_logger.info(_msg("🥳", "已点击封面完成"))
        await page.wait_for_timeout(600)

    async def _detect_landscape_cover_step(self, page: Page, cover_locator) -> bool:
        """竖封面完成后，抖音常停留在「继续设置横封面 4:3」步骤。"""
        patterns = [
            re.compile(r"继续.*(横封面|上传|设置)"),
            re.compile(r"(设置|上传).*(横封面|4\s*:\s*3)"),
            re.compile(r"横封面\s*4\s*:\s*3"),
            re.compile(r"是否继续"),
        ]
        try:
            text = (await cover_locator.inner_text(timeout=1500)) or ""
        except Exception:
            text = ""
        for pat in patterns:
            if pat.search(text):
                return True
        for name in ("设置横封面", "横封面", "横封面4:3"):
            try:
                el = cover_locator.get_by_text(name, exact=True).first
                if await el.count() and await el.is_visible():
                    return True
            except Exception:
                continue
        try:
            tip = page.get_by_text(re.compile(r"继续.*(横封面|上传)|横封面.*4\s*:\s*3")).first
            if await tip.count() and await tip.is_visible():
                return True
        except Exception:
            pass
        return False

    async def _accept_continue_landscape_prompt(self, page: Page) -> bool:
        """点「继续」进入横封面。"""
        continue_names = ("继续设置", "继续上传", "去设置", "继续", "设置横封面")
        for name in continue_names:
            try:
                btn = page.get_by_role("button", name=name, exact=True).first
                if not await btn.count():
                    btn = page.locator("button,div[role='button'],span").filter(
                        has_text=re.compile(rf"^{re.escape(name)}$")
                    ).first
                if await btn.count() and await btn.is_visible():
                    await btn.click(force=True, timeout=4000)
                    douyin_logger.info(_msg("🪟", f"已确认继续横封面: {name}"))
                    await page.wait_for_timeout(700)
                    return True
            except Exception:
                continue
        return False

    async def _skip_landscape_cover_prompt(self, page: Page) -> bool:
        """无横封面素材时跳过继续上传，避免卡在双封面步骤。"""
        skip_names = ("暂不设置", "暂不上传", "跳过", "稍后再说", "取消", "不设置", "仅使用竖封面")
        for name in skip_names:
            try:
                btn = page.get_by_role("button", name=name, exact=True).first
                if not await btn.count():
                    btn = page.locator("button,div[role='button']").filter(
                        has_text=re.compile(rf"^{re.escape(name)}$")
                    ).first
                if await btn.count() and await btn.is_visible():
                    await btn.click(force=True, timeout=4000)
                    douyin_logger.info(_msg("🪟", f"已跳过横封面步骤: {name}"))
                    await page.wait_for_timeout(700)
                    return True
            except Exception:
                continue
        return False

    async def _handle_after_cover_finish(
        self,
        page: Page,
        cover_locator,
        *,
        prefer_landscape: bool,
    ) -> str:
        """
        点「完成」后的后续态：
        confirmed / landscape / skipped_landscape / closed / unknown
        """
        cover_locator_str = "div.dy-creator-content-modal"
        for _ in range(20):
            try:
                confirm = page.get_by_text("是否确认应用此封面？", exact=False).first
                if await confirm.count() and await confirm.is_visible():
                    douyin_logger.info(_msg("🪟", "弹出确认框: 是否确认应用此封面？"))
                    btn = page.get_by_role("button", name="确定", exact=True).first
                    if not await btn.count():
                        btn = page.locator("button").filter(has_text="确定").first
                    await btn.click(force=True, timeout=5000)
                    await page.wait_for_timeout(800)
                    return "confirmed"
            except Exception:
                pass

            try:
                if await page.locator(cover_locator_str).count() == 0:
                    return "closed"
                visible_modal = page.locator(cover_locator_str).first
                if await visible_modal.count() and not await visible_modal.is_visible():
                    return "closed"
            except Exception:
                pass

            if await self._detect_landscape_cover_step(page, cover_locator):
                if prefer_landscape:
                    await self._accept_continue_landscape_prompt(page)
                    return "landscape"
                if await self._skip_landscape_cover_prompt(page):
                    return "skipped_landscape"
                return "landscape"

            await page.wait_for_timeout(350)
        return "unknown"

    async def _upload_cover_orientation(
        self,
        page: Page,
        cover_locator,
        thumb_path: str,
        orientation: str,
    ) -> bool:
        label = "竖版3:4" if orientation == "portrait" else "横版4:3"
        await self._switch_cover_orientation(cover_locator, orientation)
        await self._switch_cover_upload_tab(cover_locator)
        ok = await self._inject_cover_file(page, cover_locator, thumb_path)
        if not ok:
            douyin_logger.error(_msg("😵", f"{label}封面未能进入预览区"))
            return False
        douyin_logger.info(_msg("🖼️", f"{label}封面预览已就绪: {thumb_path}"))
        await page.wait_for_timeout(600)
        await self._click_cover_finish(page, cover_locator)
        return True

    async def _wait_cover_modal(self, page: Page, timeout_ms: int = 20000):
        """兼容抖音创作者中心封面弹层 class 变更（对齐视频号：多标题/多选择器）。"""
        selectors = (
            "div.dy-creator-content-modal",
            "div.dy-creator-content-modal-wrap",
            "[class*='dy-creator-content-modal']",
            "[class*='creator-content-modal']",
            "[class*='cover-modal']",
            "[class*='CoverModal']",
            "div.semi-modal.semi-modal-open",
            ".semi-modal:visible",
            "[role='dialog']:visible",
        )
        title_pat = re.compile(r"设置封面|选择封面|上传封面|竖封面|横封面|编辑封面|完成")
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            for sel in selectors:
                try:
                    texted = page.locator(sel).filter(has_text=title_pat).first
                    if await texted.count() and await texted.is_visible():
                        return texted
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        # 有 file input 或「完成」按钮也视为封面弹层
                        has_file = await loc.locator("input[type='file']").count()
                        has_finish = await loc.get_by_text("完成", exact=True).count()
                        if has_file or has_finish:
                            return loc
                except Exception:
                    continue
            await asyncio.sleep(0.35)
        return None

    async def _click_cover_entry(self, page: Page) -> bool:
        """多入口尝试打开封面编辑（参考视频号 open_thumbnail_dialog 多 selector）。"""
        entry_candidates = [
            page.get_by_role("button", name=re.compile(r"选择封面|设置封面|编辑封面")),
            page.get_by_text("选择封面", exact=True),
            page.get_by_text("设置封面", exact=True),
            page.get_by_text("编辑封面", exact=True),
            page.locator("[class*='cover']").filter(has_text=re.compile(r"选择封面|设置封面")).first,
            page.locator("div,span,button").filter(has_text=re.compile(r"^选择封面$")).first,
            # 封面预览缩略图区域（有时点图才能打开）
            page.locator("[class*='cover-'] img, [class*='Cover'] img, [class*='select-cover']").first,
        ]
        for loc in entry_candidates:
            try:
                target = loc.first if hasattr(loc, "first") else loc
                if not await target.count():
                    continue
                if not await target.is_visible():
                    continue
                await target.scroll_into_view_if_needed()
                await target.click(force=True, timeout=4000)
                await page.wait_for_timeout(400)
                return True
            except Exception:
                continue
        # 最后用 JS 点含「选择封面」的可点击节点
        try:
            clicked = await page.evaluate(
                """() => {
                  const nodes = [...document.querySelectorAll('button,div,span,a')];
                  const el = nodes.find(n => {
                    const t = (n.textContent || '').trim();
                    return t === '选择封面' || t === '设置封面' || t === '编辑封面';
                  });
                  if (!el) return false;
                  el.click();
                  return true;
                }"""
            )
            return bool(clicked)
        except Exception:
            return False

    async def _close_cover_modal(self, page: Page, *, saw_confirm: bool) -> bool:
        cover_locator_str = "div.dy-creator-content-modal"
        closed = False
        for state in ("hidden", "detached"):
            try:
                await page.locator(cover_locator_str).first.wait_for(state=state, timeout=8000)
                closed = True
                break
            except Exception:
                continue
        if not closed:
            try:
                btn = page.get_by_role("button", name="确定", exact=True).first
                if await btn.count() and await btn.is_visible():
                    await btn.click(force=True, timeout=3000)
                    await page.wait_for_timeout(800)
                    await page.locator(cover_locator_str).first.wait_for(state="hidden", timeout=5000)
                    closed = True
            except Exception:
                pass
        if not closed:
            try:
                if await self._skip_landscape_cover_prompt(page):
                    await page.wait_for_timeout(800)
                    await page.locator(cover_locator_str).first.wait_for(state="hidden", timeout=5000)
                    closed = True
            except Exception:
                pass
        if not closed:
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
            except Exception:
                pass
            douyin_logger.warning(
                _msg("⚠️", "封面弹窗未正常关闭；若未点到「确定」，自定义封面可能未生效")
            )
        return closed

    async def _set_thumbnail_inner(self, page: Page):
        await page.evaluate(
            "() => document.querySelectorAll('.shepherd-element,.shepherd-modal-overlay-container').forEach(e=>e.remove())"
        )
        try:
            await self._dismiss_xingtu_overlays(page)
        except Exception:
            pass

        cover_locator_str = "div.dy-creator-content-modal"
        cover_locator = None
        try:
            await page.evaluate(
                """() => {
                  const el = [...document.querySelectorAll('div,span,button')]
                    .find(n => (n.textContent || '').trim() === '选择封面');
                  if (el) el.scrollIntoView({ block: 'center' });
                }"""
            )
            await page.wait_for_timeout(400)
        except Exception:
            pass

        for attempt in range(4):
            try:
                entry = page.get_by_text("选择封面", exact=True).first
                await entry.wait_for(state="visible", timeout=12000)
                await entry.scroll_into_view_if_needed()
                await entry.click(force=True)
                await page.wait_for_selector(cover_locator_str, state="visible", timeout=12000)
                cover_locator = page.locator(cover_locator_str).first
                douyin_logger.info(_msg("🖼️", f"已打开封面弹窗（经典选择器，第{attempt + 1}次）"))
                break
            except Exception as exc:
                douyin_logger.warning(
                    _msg("⚠️", f"经典封面入口失败({attempt + 1}/4): {exc}")
                )
                try:
                    await self._dismiss_xingtu_overlays(page)
                except Exception:
                    pass
                opened = await self._click_cover_entry(page)
                if opened:
                    cover_locator = await self._wait_cover_modal(page, timeout_ms=10000)
                    if cover_locator is not None:
                        douyin_logger.info(_msg("🖼️", "已打开封面弹窗（多入口兜底）"))
                        break
                await page.wait_for_timeout(800)

        if cover_locator is None:
            raise TimeoutError("未找到封面设置弹窗（class 可能已变更）")

        await page.wait_for_timeout(1200)

        portrait_path = self.thumbnail_portrait_path
        landscape_path = self.thumbnail_landscape_path
        if portrait_path and not landscape_path:
            landscape_path = self._derive_landscape_4x3(portrait_path)
        elif landscape_path and not portrait_path:
            portrait_path = landscape_path

        saw_confirm = False
        any_uploaded = False

        # 短视频主路径：先竖封面 3:4，再横封面 4:3（完成竖封面后常提示继续横封面）
        if portrait_path:
            if not await self._upload_cover_orientation(
                page, cover_locator, portrait_path, "portrait"
            ):
                await self._dismiss_cover_modals(page)
                return
            any_uploaded = True
            state = await self._handle_after_cover_finish(
                page, cover_locator, prefer_landscape=bool(landscape_path)
            )
            if state == "confirmed":
                saw_confirm = True
            if landscape_path and state in ("landscape", "unknown", "confirmed"):
                try:
                    still_open = await page.locator(cover_locator_str).first.is_visible()
                except Exception:
                    still_open = False
                if not still_open and state == "confirmed":
                    douyin_logger.info(_msg("🧭", "竖封面已确认，重新打开弹窗设置横封面"))
                    opened = await self._click_cover_entry(page)
                    if opened:
                        cover_locator = (
                            await self._wait_cover_modal(page, timeout_ms=10000) or cover_locator
                        )
                if await self._upload_cover_orientation(
                    page, cover_locator, landscape_path, "landscape"
                ):
                    state2 = await self._handle_after_cover_finish(
                        page, cover_locator, prefer_landscape=False
                    )
                    if state2 == "confirmed":
                        saw_confirm = True
                    elif state2 == "landscape":
                        await self._skip_landscape_cover_prompt(page)
        elif landscape_path:
            if not await self._upload_cover_orientation(
                page, cover_locator, landscape_path, "landscape"
            ):
                await self._dismiss_cover_modals(page)
                return
            any_uploaded = True
            state = await self._handle_after_cover_finish(
                page, cover_locator, prefer_landscape=False
            )
            if state == "confirmed":
                saw_confirm = True

        if not any_uploaded:
            await self._dismiss_cover_modals(page)
            return

        await self._close_cover_modal(page, saw_confirm=saw_confirm)

        await page.wait_for_timeout(800)
        if await self._verify_cover_on_publish_page(page):
            extra = "，含二次确认" if saw_confirm else ""
            douyin_logger.info(_msg("🥳", f"视频封面设置完成（预览已确认{extra}）"))
        else:
            douyin_logger.warning(
                _msg("⚠️", "封面弹窗已关，但发布页未见封面缩略图，自定义封面可能未生效")
            )

    async def upload(self, playwright: Playwright) -> None:
        douyin_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        await self.validate_upload_args()
        douyin_logger.info(_msg("🥳", "上传前检查通过"))

        browser = await playwright.chromium.launch(**_douyin_browser_launch_kwargs(self.headless))
        context = await browser.new_context(
            storage_state=f"{self.account_file}",
            permissions=["geolocation"],
        )
        context = await set_init_script(context)

        page = await context.new_page()
        await page.goto("https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=180000)
        douyin_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
        douyin_logger.info(_msg("🧭", "小人正在赶往上传主页"))
        await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=90000)
        # 等待上传页就绪；若被重定向到登录页则明确报错，避免误选登录表单 input
        for wait_idx in range(60):
            if await _douyin_upload_page_ready(page):
                douyin_logger.info(_msg("✅", f"上传页就绪（等待 {wait_idx + 1}s）: {page.url}"))
                break
            if await _douyin_page_has_login_prompt(page):
                raise RuntimeError("cookie文件已失效，请先完成抖音登录")
            if wait_idx and wait_idx % 10 == 0:
                douyin_logger.info(_msg("⏳", f"等待上传页就绪... {wait_idx + 1}/60s, url={page.url}"))
            await asyncio.sleep(1)
        else:
            if await _douyin_page_has_login_prompt(page):
                raise RuntimeError("cookie文件已失效，请先完成抖音登录")
            raise RuntimeError(f"未能进入抖音上传页: {page.url}")

        upload_input = await _find_upload_file_input(page)
        douyin_logger.info(_msg("🔍", "等待上传页「上传视频」按钮或 file input..."))
        try:
            if await _douyin_upload_button_visible(page):
                douyin_logger.info(_msg("✅", "已看到「上传视频」按钮"))
            else:
                await upload_input.wait_for(state="attached", timeout=120000)
        except Exception as exc:
            if await _douyin_page_has_login_prompt(page):
                raise RuntimeError("cookie文件已失效，请先完成抖音登录") from exc
            raise RuntimeError(f"未能定位抖音上传控件: {page.url}") from exc
        douyin_logger.info(_msg("📤", "开始选择并写入视频文件"))
        await _set_douyin_upload_file(page, self.file_path)

        wait_round = 0
        while True:
            wait_round += 1
            # 抖音改版后上传完可能不自动跳 publish 页，先直接点上传页里的「发布」/「下一步」按钮
            try:
                await page.wait_for_url(
                    "https://creator.douyin.com/creator-micro/content/publish?enter_from=publish_page",
                    timeout=3000,
                )
                douyin_logger.info(_msg("🥳", "已经进入 version_1 发布页面"))
                break
            except Exception:
                try:
                    await page.wait_for_url(
                        "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page",
                        timeout=3000,
                    )
                    douyin_logger.info(_msg("🥳", "已经进入 version_2 发布页面"))
                    break
                except Exception:
                    # 主动兜底：若已离开发布/上传页（跳首页/数据中心），直接报错中止，避免死等
                    if _douyin_left_publish_page(page):
                        raise RuntimeError(
                            f"文件写入后未跳发布页，却跳转到了 {page.url}（可能抖音改版：上传完成后需手动点「发布」/「下一步」？）"
                        )
                    # 仍停在拖拽区：说明没真正选上文件，再点一次「上传视频」
                    if wait_round in (4, 20, 40) and await _douyin_upload_button_visible(page):
                        douyin_logger.warning(_msg("⚠️", "仍停在上传拖拽区，重新点击「上传视频」"))
                        try:
                            await _set_douyin_upload_file(page, self.file_path)
                        except Exception as retry_exc:
                            douyin_logger.debug(_msg("🔍", f"重试选文件失败: {retry_exc}"))
                    elif wait_round % 10 == 0:
                        await _try_click_upload_page_next(page)
                    # 每 10s 记录一次 INFO，避免日志全是 debug
                    if wait_round % 20 == 0:
                        douyin_logger.info(
                            _msg("⏳", f"等待跳转到视频发布页…（已等 {wait_round*0.5}s，当前URL: {page.url}）")
                        )
                    else:
                        douyin_logger.debug(_msg("🧍", "还没进到视频发布页面，小人继续等一会"))
                    await asyncio.sleep(0.5)

        await asyncio.sleep(1)
        douyin_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_title_and_description(page, self.title, self.desc or self.title, self.tags)
        douyin_logger.info(_msg("🏷️", f"小人一共贴了 {len(self.tags)} 个话题"))

        # ⚡ 前移自主声明设置：填完标题/话题就立即尝试（与视频上传并行，不等待上传完成）
        # 根据用户 DOM 截图：发布页 DCFF > form-container 底部的「自主声明」区在文件写入后即可点击，
        # 无需等上传完；同时发布页 DCFF 是滚动加载的，这里会先 scroll 到底部让元素渲染。
        if await _douyin_has_self_declaration_pending(page):
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                await page.wait_for_timeout(300)
            except Exception:
                pass
            douyin_logger.info(_msg("🧾", "并行设置自主声明（上传进行中同步执行，不等传完）"))
            await self.set_self_declaration(page, must_succeed=False)  # 早期失败只记 warning，上传完后再补一次
        else:
            douyin_logger.info(_msg("🧾", "自主声明已设置/无需设置（未检测到「请选择自主声明」占位）"))

        # 必须等视频传完再挂星图/设封面：传完前「选择封面」常不可用，星图弹层失败还会遮挡封面
        while True:
            try:
                number = await page.locator('[class^="long-card"] div:has-text("重新上传")').count()
                if number > 0:
                    douyin_logger.success(_msg("🥳", "视频已经传完啦"))
                    break
                douyin_logger.info(_msg("🏃", "小人正在努力上传视频"))
                await asyncio.sleep(2)
                if await page.locator('div.progress-div > div:has-text("上传失败")').count():
                    douyin_logger.error(_msg("😵", "检测到上传失败，小人准备重试"))
                    await self.handle_upload_error(page)
            except Exception:
                douyin_logger.debug(_msg("🧍", "小人还在等视频上传完成"))
                await asyncio.sleep(2)

        if self.xingtu_task_id:
            douyin_logger.info(_msg("⭐", f"小人开始挂载星图任务: {self.xingtu_task_id}"))
            try:
                await self.set_xingtu_task(page, self.xingtu_task_id)
                douyin_logger.info(_msg("🥳", "星图任务挂载成功"))
            except Exception as exc:
                # 矩阵/机会中心带星图 ID 时：挂载失败必须中止，禁止无星图发布
                try:
                    await self._dismiss_xingtu_overlays(page)
                except Exception:
                    pass
                douyin_logger.error(_msg("😵", f"星图挂载失败，中止发布: {exc}"))
                raise RuntimeError(f"星图任务挂载失败，已中止发布: {exc}") from exc

        # 合集选择：若指定了合集名称，则在发布页中勾选对应合集
        if self.collection_name:
            douyin_logger.info(_msg("📚", f"小人开始选择合集: {self.collection_name}"))
            try:
                ok = await self._select_douyin_collection(page)
                if ok:
                    douyin_logger.info(_msg("🥳", f"合集已挂上: {self.collection_name}"))
                else:
                    # 找不到合集/账号不支持合集：仅警告，不中断发布
                    douyin_logger.warning(
                        _msg(
                            "⚠️",
                            f"未能选择合集「{self.collection_name}」，视频将按无合集方式发布",
                        )
                    )
            except Exception as exc:
                douyin_logger.warning(_msg("⚠️", f"合集选择异常（继续发布）: {exc}"))

        if self.productLink and self.productTitle:
            douyin_logger.info(_msg("🛒", "小人正在设置商品链接"))
            await self.set_product_link(page, self.productLink, self.productTitle)
            douyin_logger.info(_msg("🥳", "商品链接设置完成"))

        # 星图弹层彻底关掉后再设封面，避免挡住「选择封面」
        try:
            await self._dismiss_xingtu_overlays(page)
            await page.wait_for_timeout(500)
        except Exception:
            pass
        await self.set_thumbnail(page)

        # 上传完后的补调用：只有当 pending=true（仍显示「请选择自主声明」）才设置；
        # 之前并行尝试已成功时此处直接跳过，避免入口 helper 因未命中旧文案报错中断 → 发布按钮永远走不到
        if await _douyin_has_self_declaration_pending(page):
            douyin_logger.info(_msg("🧾", "并行尝试未完成，上传完后补设自主声明"))
            await self.set_self_declaration(page)  # must_succeed=True（默认）必选
        else:
            douyin_logger.info(_msg("🧾", "自主声明已在并行阶段完成，无需重复设置（跳过补调）"))

        third_part_element = '[class^="info"] > [class^="first-part"] div div.semi-switch'
        if await page.locator(third_part_element).count():
            if "semi-switch-checked" not in await page.eval_on_selector(third_part_element, "div => div.className"):
                await page.locator(third_part_element).locator("input.semi-switch-native-control").click()

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_douyin(page, self.publish_date)

        publish_clicked = False  # ⚠️ 状态机：必须点过「发布」按钮后的跳离才算发布成功
        for publish_try in range(120):
            try:
                # 记录点击前 URL，便于事后排查"未点发布就跳转"
                url_before = page.url

                # 先判：如果本轮之前还没点过发布，但 URL 已经离开发布页 → 一定是被误点了其他元素跳走，
                # 直接报错中止，不得再重试（继续重试只会误判成功或死等）
                if not publish_clicked and _douyin_left_publish_page(page):
                    raise RuntimeError(
                        f"未点击「发布」按钮却已跳离发布页：当前 URL={page.url}（"
                        f"可能误点了「暂存离开/发布设置/取消发布」或 Esc 触发暂存离开，已中止避免假成功）"
                    )

                if publish_try == 0:
                    douyin_logger.info(_msg("🏃", f"开始点击发布，当前URL: {url_before}"))

                # 移除会拦截发布按钮点击的新手引导/封面遮罩（无封面弹层时不会按 Esc）
                # ⚠️ 不再 remove [class*="mention-wrapper"]：该选择器太宽泛，会误删话题输入区/
                #    发布设置区相关 DOM，破坏 React 组件状态 → 可能触发页面异常跳转
                await self._dismiss_cover_modals(page)
                await page.evaluate(
                    "() => { document.querySelectorAll('.shepherd-element, .shepherd-modal-overlay-container').forEach(e => e.remove()); }"
                )
                # 自主声明未选时抖音会拦发布：补一次（已选则函数内会快速跳过）
                if await _douyin_has_self_declaration_pending(page):
                    await self.set_self_declaration(page)

                # 点「下一步」优先：抖音两阶段发布页（填写信息 → 下一步 → 确认/发布），
                # 如果页面有「下一步」则先点它，进入最终发布页后再等「发布」按钮出现
                if not publish_clicked and (await _douyin_click_next_step_button(page)):
                    douyin_logger.info(
                        _msg("⏭️", f"检测到两阶段发布页，已点击「下一步」进入最终发布页（第{publish_try+1}次尝试）")
                    )
                    await asyncio.sleep(2)
                    continue

                # 点发布按钮：多选择器兜底（2026-08 抖音改版 exact=True 偶发失效）
                clicked = await _douyin_click_publish_button(page)
                if clicked:
                    publish_clicked = True  # ★ 状态机翻转：此后的跳离才算发布成功
                    douyin_logger.info(_msg("👆", f"已点击发布按钮（第{publish_try+1}次），点击前URL: {url_before}"))
                elif publish_try % 10 == 9:
                    douyin_logger.warning(
                        _msg("😵", f"未定位到「发布」按钮（第{publish_try+1}次重试），继续等待页面加载…")
                    )

                # 发布成功判断：**必须同时满足两个条件**
                #   (a) publish_clicked == True（已经真的点过「发布」按钮）
                #   (b) 离开发布/上传页（抖音改版后不一定跳到 content/manage，
                #       可能跳数据中心/创作者首页，只要 URL 不是发布页即算成功）
                if publish_clicked:
                    for _wait in range(4):  # 点后最多等 3*4=12s 确认跳转
                        if _douyin_left_publish_page(page):
                            break
                        await asyncio.sleep(3)
                    if not _douyin_left_publish_page(page):
                        raise RuntimeError(f"发布按钮点击后仍未离开发布页: {page.url}")
                    douyin_logger.success(
                        _msg("🥳", f"视频发布成功，已跳转到：{page.url}")
                    )
                    break
                else:
                    # 没点到发布就不进入等待跳转，继续重试（避免没必要地 sleep 12s）
                    await asyncio.sleep(1)
            except Exception as _exc:
                # 如果是"未点发布就跳离"这种致命错误，直接抛出，不再重试
                if "未点击「发布」按钮却已跳离发布页" in str(_exc):
                    raise
                # 关键：绑定异常并打印，否则失败原因完全不可见（之前静默吞掉导致日志只有"冲刺"无法定位）
                if publish_try < 3 or publish_try % 5 == 0:
                    douyin_logger.warning(
                        _msg("😵", f"第{publish_try+1}次发布尝试异常: {type(_exc).__name__}: {_exc} | 当前URL: {page.url}")
                    )
                await self.handle_auto_video_cover(page)
                if publish_try % 10 == 0:
                    douyin_logger.info(_msg("🏃", f"小人正在冲刺发布视频（{publish_try + 1}/120）"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError("抖音发布超时：多次点击发布仍未离开发布页（内容管理/数据中心皆未跳转）")

        await context.storage_state(path=self.account_file)
        douyin_logger.success(_msg("🥳", "cookie 更新完毕"))
        await asyncio.sleep(2)
        await context.close()
        await browser.close()

    async def douyin_upload_video(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def main(self):
        await self.douyin_upload_video()


class DouYinNote(DouYinBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        bgm: str = "",
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.image_paths = image_paths
        self.note = note or ""
        self.title = title or (self.note[:30] if self.note else "")
        self.tags = tags or []
        self.bgm = bgm or ""

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("图文模式下，title 是必须的")

        if len(self.title) > 20:
            raise ValueError(f"标题不能超过20字符，当前: {len(self.title)}字符")

        if not self.image_paths:
            raise ValueError("图文模式下，图片是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        if len(self.image_paths) > 35:
            raise ValueError("图文模式下最多只支持上传 35 张图片")

        note_len = len(self.note) if self.note else 0
        if note_len > 1000:
            raise ValueError(f"正文不能超过1000字符，当前: {note_len}字符")

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page) -> None:
        douyin_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        douyin_logger.info(_msg("🔀", "小人正在切换到图文发布"))
        await page.get_by_text("发布图文", exact=True).click()
        await page.wait_for_timeout(1000)

        douyin_logger.info(_msg("📤", "小人正在上传图片"))
        await page.locator("div[class^='container'] input[accept*='image']").set_input_files(self.image_paths)

        while True:
            try:
                await page.wait_for_url(
                    "**/creator-micro/content/post/image?**",
                    timeout=3000,
                )
                douyin_logger.info(_msg("🥳", "已经进入图文发布页面"))
                break
            except Exception:
                douyin_logger.debug(_msg("🧍", "小人还在等图片上传完成"))
                await asyncio.sleep(0.5)

        await asyncio.sleep(1)
        douyin_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_title_and_description(page, self.title, self.note, self.tags)
        title_len = len(self.title) if self.title else 0
        tags_text = " ".join(f"#{t}" for t in self.tags) if self.tags else ""
        desc_and_tags_len = len(self.note or "") + (len(tags_text) + 2 if self.tags else 0)
        douyin_logger.info(_msg("📝", f"标题总字数: {title_len}，描述+话题总字数: {desc_and_tags_len}"))
        douyin_logger.info(_msg("🏷️", f"小人一共贴了 {len(self.tags)} 个话题"))

        if self.bgm:
            await self.select_bgm(page, self.bgm)

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_douyin(page, self.publish_date)

        for publish_try in range(120):
            try:
                # 点发布按钮：多选择器兜底（2026-08 抖音改版）
                await _douyin_click_publish_button(page)
                # 发布成功判断：离开发布/上传页（抖音改版后不一定跳到 content/manage）
                for _wait in range(4):
                    if _douyin_left_publish_page(page):
                        break
                    await asyncio.sleep(3)
                if not _douyin_left_publish_page(page):
                    raise RuntimeError(f"图文发布点击后仍未离开发布页: {page.url}")
                douyin_logger.success(
                    _msg("🥳", f"图文发布成功，已跳转到：{page.url}")
                )
                break
            except Exception:
                douyin_logger.info(
                    _msg("🏃", f"小人正在冲刺发布图文（{publish_try + 1}/120）")
                )
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError("图文发布超时：多次点击发布仍未离开发布页")

    async def upload(self, playwright: Playwright) -> None:
        douyin_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        douyin_logger.info(_msg("🥳", "图文上传前检查通过"))

        browser = await playwright.chromium.launch(**_douyin_browser_launch_kwargs(self.headless))
        context = await browser.new_context(
            storage_state=f"{self.account_file}",
            permissions=["geolocation"],
        )
        context = await set_init_script(context)

        upload_success = False
        try:
            page = await context.new_page()
            await page.goto("https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=180000)
            douyin_logger.info(_msg("🧭", "小人正在赶往图文发布页"))
            await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=90000)

            await self.upload_note_content(page)
            upload_success = True
        finally:
            if upload_success:
                await context.storage_state(path=self.account_file)
                douyin_logger.success(_msg("🥳", "cookie 更新完毕"))
                await asyncio.sleep(2)
            await context.close()
            await browser.close()

    async def douyin_upload_note(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)
