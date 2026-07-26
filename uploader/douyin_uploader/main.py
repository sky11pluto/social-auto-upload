# -*- coding: utf-8 -*-
from datetime import datetime

import asyncio
import inspect
import os
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


async def _douyin_upload_input_visible(page: Page) -> bool:
    """上传页 file input 是否已挂载（比登录 Tab 文案更可靠）"""
    try:
        loc = await _find_upload_file_input(page)
        return bool(await loc.count())
    except Exception:
        return False


async def _douyin_page_has_login_prompt(page: Page) -> bool:
    """检测页面是否仍处于登录态（含慢加载时的登录文案）"""
    # 抖音上传页 DOM 常残留「扫码登录/密码登录」Tab，但 file input 已可见说明会话有效
    if await _douyin_upload_input_visible(page):
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
    """上传页就绪：URL 正确且出现上传 file input（必须，避免 cookie 失效时误判）"""
    if "content/upload" not in page.url:
        return False
    if await _douyin_page_has_login_prompt(page):
        return False
    return await _douyin_upload_input_visible(page)


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

    async def fill_title_and_description(self, page: Page, title: str, description: str, tags: list[str] | None = None):
        # 2026-06 抖音发布页 DOM：标题=input[placeholder*=填写作品标题]，描述=div.zone-container[contenteditable]
        # version_2(post/video) 发布页要等视频上传完才渲染表单（实测约 40s），故等待超时给到 120s
        title_input = page.locator('input[placeholder*="填写作品标题"]').first
        await title_input.wait_for(state="visible", timeout=120000)
        await title_input.fill(title[:30])

        description_editor = page.locator('div.zone-container[contenteditable="true"]').first
        await description_editor.wait_for(state="visible", timeout=120000)
        await description_editor.click()
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.press("Delete")

        for tag in tags or []:
            await page.keyboard.type(" #" + tag)
            await page.keyboard.press("Space")
        await page.keyboard.press("Escape")  # 收起话题下拉，避免浮层拦截后续点击

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
        """强拆封面/内容弹层，避免挡住自主声明与发布按钮。"""
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

    async def set_self_declaration(self, page: Page, declaration: str = "内容由AI生成") -> None:
        """抖音「自主声明」：按页面单选项中文文案匹配（非 id），打开声明弹窗 → 选指定类型 → 确定。

        入口和弹窗都是异步渲染，等不到就记 warning 跳过、继续发布，绝不因此中断
        （与小红书话题、视频号声明原创的容错策略保持一致）。
        """
        # 文案别名：抖音偶发空格/「为」写法差异
        declaration_aliases = [
            declaration,
            "内容由AI生成",
            "内容由 AI 生成",
            "内容为AI生成",
            "内容为 AI 生成",
        ]
        try:
            # 封面弹层未关时会拦截点击（pointer-events），先清掉
            await self._dismiss_cover_modals(page)

            # 发布页底部「自主声明」行，未选时显示占位文案「请选择自主声明」
            entry = page.get_by_text("请选择自主声明").first
            await entry.wait_for(state="visible", timeout=6000)
            await entry.click(force=True)

            # 弹窗标题「对作品内容添加声明」
            dialog = page.locator(".semi-modal-content").filter(has_text="对作品内容添加声明").first
            await dialog.wait_for(state="visible", timeout=6000)

            # 单选项：Semi 的文字是 .semi-radio-addon（常带 pointer-events:none，直接点会卡 30s 超时），
            # 要点可交互的 .semi-radio 外层；找不到外层再退回 force 强制点文字。exact 避免误命中预览「作者声明：…」。
            clicked = False
            for text in declaration_aliases:
                option = dialog.locator(".semi-radio").filter(has_text=text).first
                if await option.count():
                    await option.click(timeout=6000, force=True)
                    declaration = text
                    clicked = True
                    break
                label = dialog.get_by_text(text, exact=True).first
                if await label.count():
                    await label.click(timeout=6000, force=True)
                    declaration = text
                    clicked = True
                    break
            if not clicked:
                raise RuntimeError(f"未找到自主声明选项：{declaration_aliases[0]}")
            await dialog.get_by_role("button", name="确定").click(timeout=6000)
            await dialog.wait_for(state="hidden", timeout=6000)
            douyin_logger.info(_msg("🧾", f"自主声明已选择「{declaration}」"))
        except Exception as exc:
            douyin_logger.warning(_msg("🧾", f"自主声明设置失败，跳过该步骤继续发布：{exc}"))

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
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

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
        await page.evaluate(
            "() => document.querySelectorAll('.shepherd-element,.shepherd-modal-overlay-container').forEach(e=>e.remove())"
        )
        await page.get_by_text("选择封面", exact=True).first.click(force=True)
        cover_locator_str = "div.dy-creator-content-modal"
        cover_locator = page.locator(cover_locator_str).first
        await page.wait_for_selector(cover_locator_str, timeout=20000)
        await page.wait_for_timeout(1200)

        thumb_path = self.thumbnail_portrait_path or self.thumbnail_landscape_path
        if self.thumbnail_portrait_path:
            try:
                await cover_locator.get_by_text("设置竖封面", exact=True).first.click(timeout=3000)
                await page.wait_for_timeout(600)
            except Exception:
                pass
            orientation = "竖版"
        else:
            try:
                await cover_locator.get_by_text("设置横封面", exact=True).first.click(timeout=3000)
                await page.wait_for_timeout(600)
            except Exception:
                pass
            orientation = "横版"

        # 先切到「上传封面」页签，避免塞进 AI 参考/推荐帧
        for tab_name in ("上传封面", "本地上传"):
            try:
                tab = cover_locator.get_by_text(tab_name, exact=True).first
                if await tab.count() and await tab.is_visible():
                    await tab.click(force=True)
                    await page.wait_for_timeout(700)
                    douyin_logger.info(_msg("🧭", f"已切换封面页签「{tab_name}」"))
                    break
            except Exception:
                pass

        before = await self._cover_preview_state(cover_locator)
        uploaded = await self._upload_cover_via_file_chooser(page, cover_locator, thumb_path)
        if uploaded:
            preview_ok = False
            for _ in range(30):
                if self._cover_preview_ready(await self._cover_preview_state(cover_locator), before):
                    preview_ok = True
                    break
                await asyncio.sleep(0.4)
            uploaded = preview_ok
        if not uploaded:
            uploaded = await self._upload_cover_via_inputs(cover_locator, thumb_path)

        if not uploaded:
            douyin_logger.error(_msg("😵", "自定义封面未能进入预览区，放弃点完成以免空关弹窗"))
            await self._dismiss_cover_modals(page)
            return

        douyin_logger.info(_msg("🖼️", f"{orientation}封面预览已就绪: {thumb_path}"))
        await page.wait_for_timeout(800)

        finish_btn = cover_locator.get_by_role("button", name="完成", exact=True).first
        for _ in range(40):
            try:
                if await finish_btn.is_enabled():
                    break
            except Exception:
                pass
            await page.wait_for_timeout(500)

        await finish_btn.click()
        douyin_logger.info(_msg("🥳", "已点击封面完成"))

        saw_confirm = False
        for _ in range(15):
            try:
                confirm = page.get_by_text("是否确认应用此封面？").first
                if await confirm.is_visible():
                    douyin_logger.info(_msg("🪟", "弹出确认框: 是否确认应用此封面？"))
                    modal = page.locator("div.dy-creator-content-modal").filter(
                        has_text="是否确认应用此封面？"
                    ).first
                    btn = modal.get_by_role("button", name="确定").first
                    if await btn.count():
                        await btn.click(force=True)
                    else:
                        await page.get_by_role("button", name="确定").click(force=True)
                    saw_confirm = True
                    await page.wait_for_timeout(800)
                    break
            except Exception:
                pass
            if await page.locator(cover_locator_str).count() == 0:
                break
            await page.wait_for_timeout(400)

        closed = False
        for state in ("hidden", "detached"):
            try:
                await page.locator(cover_locator_str).first.wait_for(state=state, timeout=12000)
                closed = True
                break
            except Exception:
                continue
        if not closed:
            try:
                await page.locator("div.dy-creator-content-modal").get_by_role(
                    "button", name="确定", exact=True
                ).first.click(force=True, timeout=2000)
                await page.wait_for_timeout(800)
                await page.locator(cover_locator_str).first.wait_for(state="hidden", timeout=5000)
                closed = True
                saw_confirm = True
            except Exception:
                pass
        if not closed:
            await self._dismiss_cover_modals(page)
            douyin_logger.warning(
                _msg("⚠️", "封面弹窗未正常关闭，已强制移除遮罩；自定义封面可能未生效")
            )
            return

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
        douyin_logger.info(_msg("🔍", "等待上传页 file input 出现..."))
        try:
            await upload_input.wait_for(state="attached", timeout=120000)
        except Exception as exc:
            if await _douyin_page_has_login_prompt(page):
                raise RuntimeError("cookie文件已失效，请先完成抖音登录") from exc
            raise RuntimeError(f"未能定位抖音上传控件: {page.url}") from exc
        douyin_logger.info(_msg("📤", "已定位上传控件，开始写入视频文件"))
        await upload_input.set_input_files(self.file_path)

        while True:
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
                    douyin_logger.debug(_msg("🧍", "还没进到视频发布页面，小人继续等一会"))
                    await asyncio.sleep(0.5)

        await asyncio.sleep(1)
        douyin_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_title_and_description(page, self.title, self.desc or self.title, self.tags)
        douyin_logger.info(_msg("🏷️", f"小人一共贴了 {len(self.tags)} 个话题"))

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

        if self.productLink and self.productTitle:
            douyin_logger.info(_msg("🛒", "小人正在设置商品链接"))
            await self.set_product_link(page, self.productLink, self.productTitle)
            douyin_logger.info(_msg("🥳", "商品链接设置完成"))

        await self.set_thumbnail(page)

        await self.set_self_declaration(page)

        third_part_element = '[class^="info"] > [class^="first-part"] div div.semi-switch'
        if await page.locator(third_part_element).count():
            if "semi-switch-checked" not in await page.eval_on_selector(third_part_element, "div => div.className"):
                await page.locator(third_part_element).locator("input.semi-switch-native-control").click()

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_douyin(page, self.publish_date)

        for publish_try in range(120):
            try:
                # 移除会拦截发布按钮点击的新手引导/话题下拉/封面遮罩
                await self._dismiss_cover_modals(page)
                await page.evaluate(
                    "() => { document.querySelectorAll('.shepherd-element, .shepherd-modal-overlay-container, [class*=\"mention-wrapper\"]').forEach(e => e.remove()); }"
                )
                # 自主声明未选时抖音会拦发布：补一次（已选则入口文案变化，函数内会快速跳过/失败）
                if await page.get_by_text("请选择自主声明").count():
                    await self.set_self_declaration(page)
                publish_button = page.get_by_role("button", name="发布", exact=True)
                if await publish_button.count():
                    await publish_button.click(force=True)
                await page.wait_for_url(
                    "https://creator.douyin.com/creator-micro/content/manage**",
                    timeout=3000,
                )
                douyin_logger.success(_msg("🥳", "视频发布成功，小人开心收工"))
                break
            except Exception:
                await self.handle_auto_video_cover(page)
                if publish_try % 10 == 0:
                    douyin_logger.info(_msg("🏃", f"小人正在冲刺发布视频（{publish_try + 1}/120）"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError("抖音发布超时：多次点击发布仍未进入作品管理页")

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

        while True:
            try:
                publish_button = page.get_by_role("button", name="发布", exact=True)
                if await publish_button.count():
                    await publish_button.click()
                await page.wait_for_url(
                    "**/creator-micro/content/manage?enter_from=publish**",
                    timeout=3000,
                )
                douyin_logger.success(_msg("🥳", "图文发布成功，小人开心收工"))
                break
            except Exception:
                douyin_logger.info(_msg("🏃", "小人正在冲刺发布图文"))
                await asyncio.sleep(0.5)

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
