"""
浏览器自动化获取 reCAPTCHA token
使用 nodriver (undetected-chromedriver 继任者) 实现反检测浏览器
支持常驻模式：为每个 project_id 自动创建常驻标签页，即时生成 token
"""
import asyncio
import time
import random
import os
import sys
import re
import traceback
from typing import Optional

import nodriver as uc
from nodriver import cdp
from typing import Optional, Any, List, Dict

from ..core.logger import debug_logger


class ResidentTabInfo:
    """常驻标签页信息结构"""
    def __init__(self, tab, project_id: str):
        self.tab = tab
        self.project_id = project_id
        self.recaptcha_ready = False
        self.created_at = time.time()


class BrowserCaptchaService:
    """浏览器自动化获取 reCAPTCHA token（nodriver 有头模式）
    
    支持两种模式：
    1. 常驻模式 (Resident Mode): 为每个 project_id 保持常驻标签页，即时生成 token
    2. 传统模式 (Legacy Mode): 每次请求创建新标签页 (fallback)
    """

    _instance: Optional['BrowserCaptchaService'] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        """初始化服务"""
        self.headless = False  # nodriver 有头模式
        self.browser_instances: dict[str, Any] = {}  # account_id -> nodriver browser
        self._initialized = False
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.db = db
        
        # 常驻模式相關屬性 (account_id -> {project_id -> ResidentTabInfo})
        self._account_resident_tabs: dict[str, dict[str, ResidentTabInfo]] = {}
        self._resident_lock = asyncio.Lock()  # 保护常驻标签页操作

        # 守護進程狀態
        self._watchdog_tasks: dict[str, asyncio.Task] = {}
        self._is_shutting_down = False

    @classmethod
    async def get_instance(cls, db=None) -> 'BrowserCaptchaService':
        """获取单例实例"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
        return cls._instance

    async def get_user_agent(self, account_id: str = "default") -> str:
        """获取当前浏览器的 User-Agent
        
        [FIX] This method NO LONGER triggers browser initialization.
        It only returns a UA if the browser instance already exists.
        """
        account_id = account_id.lower()
        
        # [FIX] Check cached UA first
        if hasattr(self, f'_ua_{account_id}'):
            return getattr(self, f'_ua_{account_id}')
        
        # [FIX] Only use browser if it ALREADY EXISTS - do NOT initialize a new one
        browser = self.browser_instances.get(account_id)
        if browser:
            try:
                # 由於獲取 UA 需要一個 tab，如果已經有常駐 tab，用它
                if account_id in self._account_resident_tabs:
                    for project_id, resident_info in self._account_resident_tabs[account_id].items():
                        if resident_info and resident_info.tab:
                            ua = await resident_info.tab.evaluate("navigator.userAgent")
                            setattr(self, f'_ua_{account_id}', ua)
                            debug_logger.log_info(f"[DEBUG_UA] BrowserCaptcha found Resident UA: {ua}")
                            return ua
                
                # Try main_tab if no resident tab
                if hasattr(browser, 'main_tab') and browser.main_tab:
                    ua = await browser.main_tab.evaluate("navigator.userAgent")
                    setattr(self, f'_ua_{account_id}', ua)
                    debug_logger.log_info(f"[DEBUG_UA] BrowserCaptcha found MainTab UA: {ua}")
                    return ua
                    
            except Exception as e:
                debug_logger.log_warning(f"Failed to get UA from existing browser: {e}")
                
        # [FIX] Return fallback UA without opening any browser
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    async def initialize_for_account(self, account_id: str, create_if_missing: bool = True):
        """為特定帳號初始化 nodriver 瀏覽器
        
        [FUNDAMENTAL GUARD] This function now ALWAYS checks if a Chrome process 
        is already running for this profile FIRST, before doing anything else.
        This prevents duplicate browser windows from ANY code path.
        
        Args:
            account_id: The account identifier (email)
            create_if_missing: If False, will NOT create a new browser if none exists.
                              This is useful for read-only operations that shouldn't
                              trigger browser initialization.
        """
        # [FIX] Force lowercase
        account_id = account_id.lower()
        user_data_dir = os.path.join(os.getcwd(), "browser_data", account_id)
        
        # ============================================================
        # FUNDAMENTAL GUARD: Check Chrome process status FIRST
        # This runs regardless of whether we have a nodriver instance
        # ============================================================
        import psutil
        chrome_pid = None
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.info['name'] == 'chrome.exe':
                    cmdline = " ".join(proc.info['cmdline'] or []).lower()
                    if user_data_dir.lower() in cmdline:
                        chrome_pid = proc.info['pid']
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        if chrome_pid:
            # Check if we already have a nodriver handle for this PID
            if account_id in self.browser_instances:
                browser = self.browser_instances[account_id]
                try:
                    if not browser.stopped:
                        debug_logger.log_info(f"[BrowserCaptcha] ✓ 帳號 [{account_id}] Chrome 進程已存在且已受控 (PID: {chrome_pid})")
                        return  # All good, reuse existing instance
                except Exception:
                    pass
            
            # If we reach here, we have a Chrome process but NO controlled nodriver instance
            # This is likely a zombie from a previous session or a manual launch
            debug_logger.log_warning(f"[BrowserCaptcha] ⚠ 帳號 [{account_id}] 檢測到不受控的 Chrome 進程 (PID: {chrome_pid})，準備清理並重啟")
            try:
                p = psutil.Process(chrome_pid)
                p.kill()
                await asyncio.sleep(1) # Wait for exit
                chrome_pid = None # Clear it so we proceed to start
            except Exception as e:
                debug_logger.log_error(f"[BrowserCaptcha] ❌ 無法清理舊進程 {chrome_pid}: {e}")
                if not create_if_missing:
                    return
        
        # ============================================================
        # No Chrome running for this account
        # ============================================================
        
        # Clean up any stale nodriver instance
        if account_id in self.browser_instances:
            debug_logger.log_warning(f"[BrowserCaptcha] 帳號 [{account_id}] Chrome 已關閉，清理舊的 nodriver 實例")
            del self.browser_instances[account_id]
        
        # Check if we should create a new browser
        if not create_if_missing:
            debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] 無瀏覽器且 create_if_missing=False，跳過初始化")
            return
        
        # ============================================================
        # Create new browser (only reaches here if Chrome not running)
        # ============================================================
        try:
            debug_logger.log_info(f"[BrowserCaptcha] 正在啟動 nodriver 瀏覽器 (帳號: {account_id}, 目錄: {user_data_dir})...")

            # 確保 user_data_dir 存在
            os.makedirs(user_data_dir, exist_ok=True)

            # 啟動 nodriver 瀏覽器
            browser = await uc.start(
                headless=self.headless,
                user_data_dir=user_data_dir,
                sandbox=False,
                browser_args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-setuid-sandbox',
                    '--disable-gpu',
                    '--window-size=1280,720',
                    # '--window-position=-2000,-2000', # [FIX] Remove off-screen positioning to avoid detection
                    '--profile-directory=Default',
                    '--start-minimized',
                    '--disable-session-crashed-bubble',  # 禁用「Chrome 未正確關閉」對話框
                    '--disable-infobars',  # 禁用資訊列
                    '--hide-crash-restore-bubble',  # 隱藏恢復提示
                ]
            )

            self.browser_instances[account_id] = browser
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 帳號 [{account_id}] 的 nodriver 瀏覽器已啟動")

            # [FIX] 程式化最小化視窗
            try:
                # 使用 CDP 命令最小化視窗
                window_id = await browser.main_tab.send(cdp.browser.get_window_for_target())
                await browser.main_tab.send(cdp.browser.set_window_bounds(
                    window_id=window_id.window_id,
                    bounds=cdp.browser.Bounds(window_state=cdp.browser.WindowState.MINIMIZED)
                ))
                debug_logger.log_info(f"[BrowserCaptcha] 視窗已最小化")
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 最小化視窗失敗: {e}")

            # [FIX] 啟動時立即緩存 User-Agent，避免後續請求為了獲取 UA 而額外開窗
            try:
                # 使用主標籤頁獲取 UA
                ua = await browser.main_tab.evaluate("navigator.userAgent")
                setattr(self, f'_ua_{account_id}', ua)
                debug_logger.log_info(f"[BrowserCaptcha] User-Agent 已緩存: {ua[:30]}...")
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 啟動時緩存 UA 失敗: {e}")

            # 啟動看門狗監控
            if account_id not in self._watchdog_tasks or self._watchdog_tasks[account_id].done():
                self._watchdog_tasks[account_id] = asyncio.create_task(self._monitor_browser(account_id))

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] ❌ 帳號 [{account_id}] 瀏覽器啟動失敗: {str(e)}")
            raise

    async def _monitor_browser(self, account_id: str):
        """監控瀏覽器狀態的看門狗任務"""
        debug_logger.log_info(f"[BrowserCaptcha] 🛡️ 帳號 [{account_id}] 瀏覽器守護進程已就緒")
        try:
            while not self._is_shutting_down:
                await asyncio.sleep(5)
                
                if self._is_shutting_down:
                    break

                browser = self.browser_instances.get(account_id)
                needs_restart = False

                if not browser:
                    needs_restart = True
                else:
                    try:
                        if browser.stopped:
                            needs_restart = True
                    except Exception:
                        needs_restart = True

                if needs_restart and not self._is_shutting_down:
                    debug_logger.log_warning(f"[BrowserCaptcha] ⚠️ 檢測到帳號 [{account_id}] 的瀏覽器已關閉或無響應！")
                    debug_logger.log_info(f"[BrowserCaptcha] 🛡️ 守護進程將在 5 秒後自動重啟窗口...")
                    
                    # 清理舊標籤頁緩存，防止重啟後狀態衝突
                    async with self._resident_lock:
                        if account_id in self._account_resident_tabs:
                             self._account_resident_tabs[account_id] = {}
                             
                    await asyncio.sleep(5)
                    
                    if not self._is_shutting_down:
                        try:
                            # 重新開啟登錄窗口以維持在線
                            await self.open_login_window(account_id)
                            debug_logger.log_info(f"[BrowserCaptcha] ✅ 帳號 [{account_id}] 瀏覽器已重啟")
                        except Exception as e:
                            debug_logger.log_error(f"[BrowserCaptcha] ❌ 守護進程嘗試重啟失敗: {e}")
                            
        except asyncio.CancelledError:
            pass
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 帳號 [{account_id}] 守護進程異常退出: {e}")

    # ========== 常驻模式 API ==========

    # start_resident_mode and stop_resident_mode are removed as per the diff,
    # as the resident mode is now managed per account/project dynamically within get_token.

    async def _wait_for_recaptcha(self, tab, timeout_loops: int = 20) -> bool:
        """等待 reCAPTCHA 加载
        
        Returns:
            True if reCAPTCHA loaded successfully
        """
        debug_logger.log_info("[BrowserCaptcha] 检测 reCAPTCHA...")
        
        # 检查 grecaptcha.enterprise.execute
        is_enterprise = await tab.evaluate(
            "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && typeof grecaptcha.enterprise.execute === 'function'"
        )
        
        if is_enterprise:
            debug_logger.log_info("[BrowserCaptcha] reCAPTCHA Enterprise 已加载")
            return True
        
        # 尝试注入脚本
        debug_logger.log_info("[BrowserCaptcha] 未检测到 reCAPTCHA，注入脚本...")
        
        await tab.evaluate(f"""
            (() => {{
                if (document.querySelector('script[src*="recaptcha"]')) return;
                const script = document.createElement('script');
                script.src = 'https://www.google.com/recaptcha/api.js?render={self.website_key}';
                script.async = true;
                document.head.appendChild(script);
            }})()
        """)
        
        # 等待脚本加载
        await tab.sleep(3)
        
        # 轮询等待 reCAPTCHA 加载
        for i in range(timeout_loops):
            is_enterprise = await tab.evaluate(
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && typeof grecaptcha.enterprise.execute === 'function'"
            )
            
            if is_enterprise:
                debug_logger.log_info(f"[BrowserCaptcha] reCAPTCHA Enterprise 已加载（等待了 {i * 0.5} 秒）")
                return True
            await tab.sleep(0.5)
        
        debug_logger.log_warning("[BrowserCaptcha] reCAPTCHA 加载超时")
        return False

    async def _execute_recaptcha_on_tab(self, tab, action: str = "IMAGE_GENERATION") -> Optional[str]:
        debug_logger.log_info(f"[DEBUG_ACTION] Executing reCAPTCHA with action: {action}")
        """在指定标签页执行 reCAPTCHA 获取 token
        
        Args:
            tab: nodriver 标签页对象
            action: reCAPTCHA action类型 (IMAGE_GENERATION 或 VIDEO_GENERATION)
            
        Returns:
            reCAPTCHA token 或 None
        """
        # [FIX] 移除 bring_to_front()。獲取 Token 不需要將視窗置頂，
        # 頻繁置頂會造成使用者操作時的「跳轉」與干擾。
        # try:
        #     await tab.bring_to_front()
        #     await asyncio.sleep(random.uniform(0.5, 1.5))
        # except:
        #     pass
            
        # 生成唯一变量名避免冲突
        ts = int(time.time() * 1000)
        token_var = f"_recaptcha_token_{ts}"
        error_var = f"_recaptcha_error_{ts}"
        
        execute_script = f"""
            (() => {{
                window.{token_var} = null;
                window.{error_var} = null;
                
                try {{
                    grecaptcha.enterprise.ready(function() {{
                        // 稍微延迟执行，模拟人类反应
                        setTimeout(() => {{
                            grecaptcha.enterprise.execute('{self.website_key}', {{action: '{action}'}})
                                .then(function(token) {{
                                    window.{token_var} = token;
                                }})
                                .catch(function(err) {{
                                    window.{error_var} = err.message || 'execute failed';
                                }});
                        }}, {random.randint(100, 500)});
                    }});
                }} catch (e) {{
                    window.{error_var} = e.message || 'exception';
                }}
            }})()
        """
        
        # 注入执行脚本
        await tab.evaluate(execute_script)
        
        # 轮询等待结果（最多 20 秒，因为增加了延迟）
        token = None
        for i in range(40):
            await tab.sleep(0.5)
            token = await tab.evaluate(f"window.{token_var}")
            if token:
                break
            error = await tab.evaluate(f"window.{error_var}")
            if error:
                debug_logger.log_error(f"[BrowserCaptcha] reCAPTCHA 错误: {error}")
                break
        
        # 清理临时变量
        try:
            await tab.evaluate(f"delete window.{token_var}; delete window.{error_var};")
        except:
            pass
        
        return token

    # ========== 主要 API ==========

    async def get_token(self, project_id: str, account_id: str, action: str = "IMAGE_GENERATION", st: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        """获取 reCAPTCHA token
        
        自动常驻模式：如果该 project_id 没有常駐標籤頁，則自動創建並常駐
        
        Args:
            project_id: Flow項目ID
            account_id: 賬號標識
            action: reCAPTCHA action類型
            st: 選擇性的 Session Token (用於緩存失效後的注入)

        Returns:
            Tuple[Optional[str], Optional[str]]: (reCAPTCHA token, Full Cookie String)
        """
        # [FIX] Force lowercase
        if not account_id: account_id = "default"
        account_id = account_id.lower()
        
        # 确保浏览器已初始化
        await self.initialize_for_account(account_id)
        browser = self.browser_instances[account_id]
        
        # 尝试从常驻标签页获取 token
        async with self._resident_lock:
            if account_id not in self._account_resident_tabs:
                self._account_resident_tabs[account_id] = {}
            
            resident_info = self._account_resident_tabs[account_id].get(project_id)
            
            # 如果该 project_id 没有常驻标签页，则自动创建
            if resident_info is None:
                debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] project_id={project_id} 沒有常駐標籤頁，正在創建唯一分頁...")
                # 我們直接在鎖內建立對象並標記，防止併發請求開出多個分頁
                resident_info = ResidentTabInfo(None, project_id)
                self._account_resident_tabs[account_id][project_id] = resident_info
                
                # 在鎖內進行導航（鎖的時間會變長，但能保證唯一性）
                try:
                    # 如果 browser 實例還沒準備好，先確保它存在
                    if browser is None:
                        await self.initialize_for_account(account_id)
                        browser = self.browser_instances[account_id]
                    
                    # 開始導航 (標記來源為 API_GET_TOKEN，並傳遞 ST 以備注入)
                    success = await self._navigate_resident_tab(resident_info, browser, caller="API_GET_TOKEN", st=st)
                    if not success:
                        debug_logger.log_error(f"[BrowserCaptcha] 帳號 [{account_id}] 首次導航失敗")
                        del self._account_resident_tabs[account_id][project_id]
                        return None, None
                except Exception as e:
                    debug_logger.log_error(f"[BrowserCaptcha] 帳號 [{account_id}] 創建分頁異常: {e}")
                    if project_id in self._account_resident_tabs[account_id]:
                        del self._account_resident_tabs[account_id][project_id]
                    return None, None
                
                debug_logger.log_info(f"[BrowserCaptcha] ✅ 帳號 [{account_id}] 已為 project_id={project_id} 成功創建並穩定停留")
        
        # 使用常驻标签页生成 token
        if resident_info and resident_info.tab:
            # [FIX] 每次獲取 token 前都進行輕量級網址檢查/恢復 (Regex 比對 + Session 注入)
            # 這能處理標籤頁在背景因 Session 過期被 Google 重定向到 [projectId] 的情況
            await self._navigate_resident_tab(resident_info, browser, caller="API_GET_TOKEN_STABLE", st=st)
            
            start_time = time.time()
            debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] 正在常駐標籤頁生成 token (project: {project_id})...")
            try:
                # [FIX] 鎖定在第一個分頁中執行，不再進行重建或回退，徹底避免「跳轉第二次」
                token = await self._execute_recaptcha_on_tab(resident_info.tab, action)
                cookies = await self._get_full_cookies(resident_info.tab)
                
                if token:
                    duration_ms = (time.time() - start_time) * 1000
                    debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] ✅ Token生成成功（耗時 {duration_ms:.0f}ms）")
                    return token, cookies
                else:
                    debug_logger.log_error(f"[BrowserCaptcha] 帳號 [{account_id}] 常駐標籤頁獲取 Token 失敗 (返回空值)")
                    return None, None
            except Exception as e:
                debug_logger.log_error(f"[BrowserCaptcha] 帳號 [{account_id}] 常駐分頁操作異常: {e}")
                return None, None
        
        return None, None
        
        return None, None

    async def _navigate_resident_tab(self, resident_info: ResidentTabInfo, browser, caller: str = "UNKNOWN", st: Optional[str] = None) -> bool:
        """為指定 ResidentTabInfo 進行導航、初始化與 Session 注入
        
        Args:
            resident_info: 預先分配的 ResidentTabInfo 對象
            browser: nodriver 瀏覽器實例
            caller: 呼叫來源標籤 (e.g., API, WATCHDOG, REFRESH)
            st: 選配的 Session Token 用於注入注入失效的 Session
            
        Returns:
            bool: 是否初始化成功
        """
        project_id = resident_info.project_id
        try:
            sys.stderr.write(f"\n[DEBUG_TRACE] [{caller}] Entering _navigate_resident_tab. ProjectID: {project_id}\n")
            # [REVERTED] Use project-specific URL as requested by user.
            website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
            debug_logger.log_info(f"[BrowserCaptcha] [{caller}] 為 project_id={project_id} 導航，目標: {website_url}")
            sys.stderr.write(f"[DEBUG_TRACE] [{caller}] Targeted URL: {website_url}\n")
            
            # [FIX] 強化導航鎖定：使用正則表達式尋找已經在目標網址的分頁 (容忍語系路徑如 /zh/ /en/)
            # 目標模式: https://labs.google/fx/(語系/)?tools/flow/project/{project_id}
            url_pattern = f"labs\\.google/fx/(?:[a-z]{{2}}(?:-[a-z]{{2}})?/)?tools/flow/project/{re.escape(project_id)}"
            tab = None
            if browser.tabs:
                for t in browser.tabs:
                    try:
                        curr_url = await t.evaluate("window.location.href")
                        if re.search(url_pattern, curr_url):
                            # [FIX] 如果匹配了網址但卻是 [projectId] 模板，代表 Session 失效，需要透過 injection 恢復
                            if "[projectId]" in curr_url or "accounts.google.com" in curr_url:
                                sys.stderr.write(f"[DEBUG_TRACE] [{caller}] Tab matches pattern but session EXPIRED (at {curr_url}). Skipping this tab.\n")
                                continue
                                
                            tab = t
                            sys.stderr.write(f"[DEBUG_TRACE] [{caller}] FOUND EXISTING tab matching pattern. Skipping navigation.\n")
                            break
                    except:
                        continue
                
                if not tab:
                    tab = browser.tabs[0]
                    try:
                        curr_url = await tab.evaluate("window.location.href")
                        # 1. 檢查是否已经在正確的專案（不管是哪個語系）
                        if re.search(url_pattern, curr_url):
                            sys.stderr.write(f"[DEBUG_TRACE] [{caller}] Tab0 matches pattern. Skipping physical get().\n")
                        
                        # 2. [FIX] Session 注入：如果目前在登錄頁面或 [projectId] 模板頁面，代表 Session 失效
                        elif "accounts.google.com" in curr_url or "[projectId]" in curr_url:
                            sys.stderr.write(f"[DEBUG_TRACE] [{caller}] Session potentially EXPIRED (at {curr_url}).\n")
                            
                            if st:
                                sys.stderr.write(f"[DEBUG_TRACE] [{caller}] [V2-CDP] Attempting SESSION INJECTION (ST found)...\n")
                                # 注入 Cookie (針對 labs.google)
                                # 僅對 labs.google 域名設置 __Secure-next-auth.session-token
                                try:
                                    # [FIX] 改用底層 CDP 指令設置 Cookie，避開版本不相容問題
                                    from nodriver import cdp
                                    await tab.send(cdp.network.set_cookie(
                                        name="__Secure-next-auth.session-token",
                                        value=st,
                                        domain="labs.google",
                                        path="/",
                                        secure=True,
                                        http_only=True
                                    ))
                                    # [FIX] 增加緩衝時間，確保 Cookie 注入生效
                                    await asyncio.sleep(1)
                                    sys.stderr.write(f"[DEBUG_TRACE] [{caller}] Injection done. Navigating to Project UUID: {website_url}\n")
                                    await tab.get(website_url)
                                except Exception as e_inj:
                                    sys.stderr.write(f"[DEBUG_TRACE] [{caller}] Injection FAILED: {e_inj}. Falling back to normal nav.\n")
                                    await tab.get(website_url)
                            else:
                                # 無 ST 可用，跳轉到儀表板引導手動登錄或防止死循環
                                sys.stderr.write(f"[DEBUG_TRACE] [{caller}] NO ST available for injection. Falling back to Dashboard.\n")
                                await tab.get("https://labs.google/fx/tools/flow")
                        
                        # 3. 其他網址不匹配，正常導航
                        else:
                            sys.stderr.write(f"[DEBUG_TRACE] [{caller}] URL MISMATCH! Current: {curr_url}. Navigating to: {website_url}\n")
                            await tab.get(website_url)
                    except Exception as e:
                        sys.stderr.write(f"[DEBUG_TRACE] [{caller}] Evaluate URL failed, forcing nav: {e}\n")
                        await tab.get(website_url)
            else:
                sys.stderr.write(f"[DEBUG_TRACE] [{caller}] Opening NEW tab for: {website_url}\n")
                tab = await browser.get(website_url, new_tab=True)
            
            resident_info.tab = tab
            sys.stderr.write(f"[DEBUG_TRACE] Waiting for load...\n")
            
            # 等待页面加载完成
            page_loaded = False
            for retry in range(60):
                try:
                    await asyncio.sleep(1)
                    ready_state = await tab.evaluate("document.readyState")
                    if ready_state == "complete":
                        page_loaded = True
                        sys.stderr.write(f"[DEBUG_TRACE] Page loaded (retry {retry})\n")
                        break
                except ConnectionRefusedError as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] 标签页连接丢失: {e}")
                    sys.stderr.write(f"[DEBUG_TRACE] ConnectionRefusedError: {e}\n")
                    return False
                except Exception as e:
                    sys.stderr.write(f"[DEBUG_TRACE] Page load exception: {e}\n")
                    await asyncio.sleep(1)
            
            if not page_loaded:
                sys.stderr.write(f"[DEBUG_TRACE] Page load TIMEOUT\n")
                debug_logger.log_error(f"[BrowserCaptcha] 页面加载超时 (project: {project_id})")
                try: await tab.close()
                except: pass
                return False
            
            # [DEBUG] Log actual page URL after load to verify no unexpected redirect occurred
            try:
                actual_url = await tab.evaluate("window.location.href")
                sys.stderr.write(f"[DEBUG_TRACE] Actual URL after load: {actual_url}\n")
                debug_logger.log_info(f"[BrowserCaptcha] [DEBUG] 页面加载完成，實際 URL: {actual_url}")
            except Exception as e:
                sys.stderr.write(f"[DEBUG_TRACE] Failed to get content URL: {e}\n")
            
            # 等待 reCAPTCHA 加载
            sys.stderr.write(f"[DEBUG_TRACE] Calling _wait_for_recaptcha...\n")
            recaptcha_ready = await self._wait_for_recaptcha(tab, timeout_loops=60) # Increased to 30s
            sys.stderr.write(f"[DEBUG_TRACE] _wait_for_recaptcha result: {recaptcha_ready}\n")
            
            if not recaptcha_ready:
                sys.stderr.write(f"[DEBUG_TRACE] Recaptcha NOT ready. Closing tab.\n")
                debug_logger.log_error(f"[BrowserCaptcha] reCAPTCHA 加载失败 (project: {project_id})")
                try:
                    await tab.close()
                except:
                    pass
                return False
            
            # [FIX] CRITICAL: The object is already created, just update its flag!
            resident_info.recaptcha_ready = True # We already verified it above
            
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 常駐標籤頁初始化成功 (project: {project_id})")
            return True
            
        except Exception as e:
            sys.stderr.write(f"[DEBUG_TRACE] _navigate_resident_tab EXCEPTION: {e}\n")
            traceback.print_exc()
            debug_logger.log_error(f"[BrowserCaptcha] 初始化常駐標籤頁異常: {e}")
            if tab:
                try: await tab.close()
                except: pass
            return False

    async def _close_resident_tab(self, account_id: str, project_id: str):
        """关闭指定 project_id 的常駐標籤頁"""
        if account_id in self._account_resident_tabs:
            resident_info = self._account_resident_tabs[account_id].pop(project_id, None)
            if resident_info and resident_info.tab:
                try:
                    await resident_info.tab.close()
                    debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] 已關閉 project_id={project_id} 的常駐標籤頁")
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] 帳號 [{account_id}] 關閉標籤頁時異常: {e}")

    async def _get_token_legacy(self, browser, project_id: str, account_id: str, action: str = "IMAGE_GENERATION") -> tuple[Optional[str], Optional[str]]:
        sys.stderr.write(f"\n[DEBUG_TRACE] Entering _get_token_legacy. ProjectID: {project_id}\n")
        """传统模式获取 reCAPTCHA token（每次创建新标签页）"""
        start_time = time.time()
        tab = None

        try:
            # [REVERTED] Use project-specific URL for legacy mode as requested.
            website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
            sys.stderr.write(f"[DEBUG_TRACE] Legacy Target URL: {website_url} (Project Specific)\n")
            debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] [Legacy] 訪問項目頁面: {website_url} (Project ID: {project_id})")
            
            # Sanity check for project_id (just logging)
            if not project_id or "project" in str(project_id).lower() or "[" in str(project_id):
                 debug_logger.log_warning(f"[BrowserCaptcha] [Legacy] ⚠️ 注意: Project ID 格式不尋常: {project_id}")

            # 新建标签页并访问页面
            tab = await browser.get(website_url, new_tab=True)

            # 等待页面完全加载（增加等待时间）
            debug_logger.log_info("[BrowserCaptcha] [Legacy] 等待页面加载...")
            await tab.sleep(3)
            
            # 等待页面 DOM 完成
            for _ in range(10):
                ready_state = await tab.evaluate("document.readyState")
                if ready_state == "complete":
                    break
                await tab.sleep(0.5)

            # 等待 reCAPTCHA 加载
            recaptcha_ready = await self._wait_for_recaptcha(tab, timeout_loops=60) # Increased timeout

            if not recaptcha_ready:
                debug_logger.log_error(f"[BrowserCaptcha] [Legacy] reCAPTCHA 无法加载 (project: {project_id})")
                return None

            # 执行 reCAPTCHA
            debug_logger.log_info(f"[BrowserCaptcha] [Legacy] 执行 reCAPTCHA 验证 (action: {action})...")
            token = await self._execute_recaptcha_on_tab(tab, action)

            duration_ms = (time.time() - start_time) * 1000

            if token:
                # [FIX] Get full cookies
                cookies = await self._get_full_cookies(tab)
                debug_logger.log_info(f"[BrowserCaptcha] [Legacy] ✅ Token获取成功（耗时 {duration_ms:.0f}ms）")
                return token, cookies
            else:
                debug_logger.log_error("[BrowserCaptcha] [Legacy] Token获取失败（返回null）")
                return None, None

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] [Legacy] 获取token异常: {str(e)}")
            return None, None
        finally:
            # 关闭标签页（但保留浏览器）
            if tab:
                try:
                    await tab.close()
                except Exception:
                    pass
    async def _get_full_cookies(self, tab) -> Optional[str]:
        """使用 CDP 獲取相關域名的所有 Cookie 並格式化"""
        try:
            # [FIX] 使用更底層但也更穩定的方式獲取所有 Cookie
            cookies_obj = await tab.send(cdp.network.get_all_cookies())
            
            if not cookies_obj:
                sys.stderr.write("[DEBUG_TRACE] _get_full_cookies: No cookies found!\n")
                return None
            
            # 篩選僅與 Google 相關的 Cookie，避免 Header 過大
            allowed_domains = [".google.com", "labs.google", "google.com", "www.google.com"]
            
            cookie_list = []
            st_found = False
            
            for cookie in cookies_obj:
                # 檢查域名是否匹配
                match = False
                for domain in allowed_domains:
                    if domain in cookie.domain:
                        match = True
                        break
                
                if not match:
                    continue
                
                # 格式化: name=value
                cookie_list.append(f"{cookie.name}={cookie.value}")
                
                if "__Secure-next-auth.session-token" in cookie.name:
                    st_found = True
            
            if not cookie_list:
                return None
                
            full_cookies = "; ".join(cookie_list)
            sys.stderr.write(f"[DEBUG_TRACE] _get_full_cookies: Filtered to {len(cookie_list)}/56+ cookies. ST_Found: {st_found}\n")
            return full_cookies
        except Exception as e:
            sys.stderr.write(f"[DEBUG_TRACE] _get_full_cookies EXCEPTION: {e}\n")
            return None

    async def close(self):
        """关闭所有浏览器实例"""
        self._is_shutting_down = True
        debug_logger.log_info("[BrowserCaptcha] 正在關閉瀏覽器服務並停止守護進程...")
        
        # 取消所有看門狗
        for account_id, task in self._watchdog_tasks.items():
            if not task.done():
                task.cancel()
        
        try:
            async with self._resident_lock:
                for account_id in list(self.browser_instances.keys()):
                    await self.stop_all_for_account(account_id)
            debug_logger.log_info("[BrowserCaptcha] 所有瀏覽器執行個體已關閉")
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 關閉瀏覽器異常: {str(e)}")

    async def stop_all_for_account(self, account_id: str):
        """關閉特定帳號的所有資源"""
        # 關閉常駐標籤頁
        if account_id in self._account_resident_tabs:
            for project_id in list(self._account_resident_tabs[account_id].keys()):
                await self._close_resident_tab(account_id, project_id)
            del self._account_resident_tabs[account_id]
            
        # 關閉瀏覽器
        browser = self.browser_instances.pop(account_id, None)
        if browser:
            try:
                browser.stop()
            except Exception:
                pass

    async def keep_alive_all_tabs(self):
        """主動對所有常駐標籤頁進行刷新，防止 Session 被 Google 判定為閒置"""
        # [FIX] 暫時關閉全域 reload，因為這會導致使用者看到的視窗被意外刷新跳轉。
        # 改為執行輕量級指令，只要讓瀏覽器有活動即可。
        debug_logger.log_info("[BrowserCaptcha] 正在執行輕量級標籤頁保活 (Activity Only)...")
        async with self._resident_lock:
            for account_id, projects in self._account_resident_tabs.items():
                for project_id, resident_info in projects.items():
                    if resident_info and resident_info.tab:
                        try:
                            # [FIX] 改用 evaluate 而非 reload，徹底解決「第二次跳轉」的問題
                            await resident_info.tab.evaluate("console.log('Keep-alive check')")
                            # 隨機等待 1s，維持心跳感
                            await asyncio.sleep(1)
                        except Exception as e:
                            debug_logger.log_warning(f"[BrowserCaptcha] 帳號 [{account_id}] 保活失敗: {e}")

    async def _minimize_window(self, account_id: str):
        """強制最小化特定帳號的瀏覽器視窗"""
        browser = self.browser_instances.get(account_id)
        if not browser:
            return
        try:
            # 使用 CDP 命令強制最小化
            window_id = await browser.main_tab.send(cdp.browser.get_window_for_target())
            await browser.main_tab.send(cdp.browser.set_window_bounds(
                window_id=window_id.window_id,
                bounds=cdp.browser.Bounds(window_state=cdp.browser.WindowState.MINIMIZED)
            ))
        except Exception:
            pass

    async def open_login_window(self, account_id: str = "default"):
        """打开登录窗口供用户手动登录 Google"""
        account_id = account_id.lower()
        await self.initialize_for_account(account_id)
        browser = self.browser_instances[account_id]
        # [FIX] 導向 Flow 首頁而非直接導向 Google 登錄，增加穩定感
        tab = await browser.get("https://labs.google/fx/tools/flow")
        debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] 已打開 Flow 儀表板窗口。")

    # ========== Session Token 刷新 ==========

    async def refresh_session_token(self, project_id: str, account_id: str = "default") -> Optional[str]:
        """从常驻标签页获取最新的 Session Token"""
        account_id = account_id.lower()
        # 确保浏览器已初始化
        await self.initialize_for_account(account_id)
        browser = self.browser_instances[account_id]
        
        start_time = time.time()
        debug_logger.log_info(f"[BrowserCaptcha] 开始刷新 Session Token (project: {project_id})...")
        
        # 尝试获取或创建常驻标签页
        async with self._resident_lock:
            if account_id not in self._account_resident_tabs:
                self._account_resident_tabs[account_id] = {}
            
            resident_info = self._account_resident_tabs[account_id].get(project_id)
            
            # 如果该 project_id 没有常驻标签页，则创建
            if resident_info is None:
                debug_logger.log_info(f"[BrowserCaptcha] [REFRESH_ST] 帳號 [{account_id}] project_id={project_id} 沒有常駐標籤頁，正在創建...")
                # 我們直接在鎖內建立對象並標記，防止併發請求開出多個分頁
                resident_info = ResidentTabInfo(None, project_id)
                self._account_resident_tabs[account_id][project_id] = resident_info
                
                # REFRESH_ST 時不一定有 new st，但導航邏輯會處理基本跳轉
                success = await self._navigate_resident_tab(resident_info, browser, caller="REFRESH_ST")
                if not success:
                    debug_logger.log_warning(f"[BrowserCaptcha] 帳號 [{account_id}] 無法為 project_id={project_id} 創建常駐標籤頁")
                    del self._account_resident_tabs[account_id][project_id]
                    return None
        
        if not resident_info or not resident_info.tab:
            debug_logger.log_error(f"[BrowserCaptcha] 无法获取常驻标签页")
            return None
        
        tab = resident_info.tab
        
        try:
            # [FIX] 移除 tab.reload()。獲取 Cookies 不需要重新整理頁面，
            # 頻繁 reload 會導致使用者正在操作的視窗發生意外跳轉。
            debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] 獲取當前分頁 cookies (不進行 reload)")
            
            # 等待一小段時間確保非同步狀態穩定
            await asyncio.sleep(1)
            
            # 从 cookies 中提取 __Secure-next-auth.session-token
            # nodriver 可以通过 browser 获取 cookies
            session_token = None
            
            try:
                # 使用 nodriver 的 cookies API 获取所有 cookies
                cookies = await browser.cookies.get_all()
                
                for cookie in cookies:
                    if cookie.name == "__Secure-next-auth.session-token":
                        session_token = cookie.value
                        break
                        
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 帳號 [{account_id}] 通过 cookies API 获取失败: {e}，尝试从 document.cookie 获取...")
                
                # 备选方案：通过 JavaScript 获取 (注意：HttpOnly cookies 可能无法通过此方式获取)
                try:
                    all_cookies = await tab.evaluate("document.cookie")
                    if all_cookies:
                        for part in all_cookies.split(";"):
                            part = part.strip()
                            if part.startswith("__Secure-next-auth.session-token="):
                                session_token = part.split("=", 1)[1]
                                break
                except Exception as e2:
                    debug_logger.log_error(f"[BrowserCaptcha] 帳號 [{account_id}] document.cookie 获取失败: {e2}")
            
            duration_ms = (time.time() - start_time) * 1000
            
            if session_token:
                debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] ✅ Session Token 获取成功（耗时 {duration_ms:.0f}ms）")
                return session_token
            else:
                debug_logger.log_error(f"[BrowserCaptcha] 帳號 [{account_id}] ❌ 未找到 __Secure-next-auth.session-token cookie")
                return None
                
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 帳號 [{account_id}] 刷新 Session Token 異常: {str(e)}")
            
            # 常驻标签页可能已失效，嘗試重新導航
            async with self._resident_lock:
                # 不再重建對象，直接導航現有分頁
                resident_info = self._account_resident_tabs.get(account_id, {}).get(project_id)
                if resident_info and resident_info.tab:
                    success = await self._navigate_resident_tab(resident_info, browser, caller="REFRESH_ST_RETRY")
                    if success:
                        # 再次嘗試獲取 Cookie
                        try:
                            cookies = await browser.cookies.get_all()
                            for cookie in cookies:
                                if cookie.name == "__Secure-next-auth.session-token":
                                    debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] ✅ 重刷後 Session Token 獲獲成功")
                                    return cookie.value
                        except Exception:
                            pass
            
            return None

    # ========== 状态查询 ==========
 
    def is_resident_mode_active(self, account_id: Optional[str] = None) -> bool:
        """检查是否有任何常驻标签页激活"""
        if account_id:
            return len(self._account_resident_tabs.get(account_id, {})) > 0
        return any(len(tabs) > 0 for tabs in self._account_resident_tabs.values())
 
    def get_resident_count(self, account_id: Optional[str] = None) -> int:
        """获取当前常驻标签页数量"""
        if account_id:
            return len(self._account_resident_tabs.get(account_id, {}))
        return sum(len(tabs) > 0 for tabs in self._account_resident_tabs.values())
 
    def get_resident_project_ids(self, account_id: str) -> list[str]:
        """获取所有当前常驻的 project_id 列表"""
        return list(self._account_resident_tabs.get(account_id, {}).keys())

    def get_resident_project_id(self, account_id: str) -> Optional[str]:
        """获取当前常驻的 project_id（向后兼容，返回第一个）"""
        if account_id in self._account_resident_tabs and self._account_resident_tabs[account_id]:
            return next(iter(self._account_resident_tabs[account_id].keys()))
        return None