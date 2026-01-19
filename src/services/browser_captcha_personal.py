"""
浏览器自动化获取 reCAPTCHA token
使用 nodriver (undetected-chromedriver 继任者) 实现反检测浏览器
支持常驻模式：为每个 project_id 自动创建常驻标签页，即时生成 token
"""
import asyncio
import time
import os
from typing import Optional

import nodriver as uc
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
        """获取当前浏览器的 User-Agent"""
        await self.initialize_for_account(account_id)
        browser = self.browser_instances.get(account_id)
        if browser:
            # 简单方式：通过 evaluate 获取 (nodriver browser 对象没有直接的 ua 属性，通常需要通过 tab)
            # 我們可以嘗試從配置中獲取，或者打開一個臨時標籤頁
            try:
                # 為了避免頻繁打開標籤頁，我們可以緩存它
                if hasattr(self, f'_ua_{account_id}'):
                    return getattr(self, f'_ua_{account_id}')
                
                # 由於獲取 UA 需要一個 tab，如果已經有常駐 tab，用它
                project_id = self.get_resident_project_id(account_id)
                if project_id:
                     resident_info = self._account_resident_tabs[account_id][project_id]
                     if resident_info and resident_info.tab:
                         ua = await resident_info.tab.evaluate("navigator.userAgent")
                         setattr(self, f'_ua_{account_id}', ua)
                         return ua
                
                # 否則新建一個 (這可能會慢一點)
                tab = await browser.get("about:blank", new_tab=True)
                ua = await tab.evaluate("navigator.userAgent")
                await tab.close()
                setattr(self, f'_ua_{account_id}', ua)
                return ua
            except Exception as e:
                debug_logger.log_warning(f"Failed to get UA from browser: {e}")
                
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    async def initialize_for_account(self, account_id: str):
        """為特定帳號初始化 nodriver 瀏覽器"""
        if account_id in self.browser_instances:
            browser = self.browser_instances[account_id]
            # 检查浏览器是否仍然存活
            try:
                # 尝试获取浏览器信息验证存活
                if browser.stopped:
                    debug_logger.log_warning(f"[BrowserCaptcha] 帳號 [{account_id}] 瀏覽器已停止，重新初始化...")
                    del self.browser_instances[account_id] # 清理旧的实例
                else:
                    return # 浏览器仍然存活，无需重新初始化
            except Exception:
                debug_logger.log_warning(f"[BrowserCaptcha] 帳號 [{account_id}] 瀏覽器無響應，重新初始化...")
                del self.browser_instances[account_id] # 清理旧的实例

        try:
            user_data_dir = os.path.join(os.getcwd(), "browser_data", account_id)
            debug_logger.log_info(f"[BrowserCaptcha] 正在啟動 nodriver 瀏覽器 (帳號: {account_id}, 目錄: {user_data_dir})...")

            # 確保 user_data_dir 存在
            os.makedirs(user_data_dir, exist_ok=True)

            # [清理性優化] 啟動前先檢查並殺失掉可能殘留的相同 Profile 瀏覽器進程
            import psutil
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if proc.info['name'] == 'chrome.exe':
                        cmdline = " ".join(proc.info['cmdline'] or []).lower()
                        # 如果命令行包含當前帳號的 profile 目錄，則將其殺掉
                        if user_data_dir.lower() in cmdline:
                            debug_logger.log_info(f"[BrowserCaptcha] 發現殘留進程 (PID: {proc.info['pid']})，正在清理...")
                            proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

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
                    '--profile-directory=Default',
                    '--start-minimized',
                ]
            )

            self.browser_instances[account_id] = browser
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 帳號 [{account_id}] 的 nodriver 瀏覽器已啟動")

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

    async def _wait_for_recaptcha(self, tab) -> bool:
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
        for i in range(20):
            is_enterprise = await tab.evaluate(
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && typeof grecaptcha.enterprise.execute === 'function'"
            )
            
            if is_enterprise:
                debug_logger.log_info(f"[BrowserCaptcha] reCAPTCHA Enterprise 已加载（等待了 {i * 0.5} 秒）")
                return True
            await tab.sleep(0.5)
        
        debug_logger.log_warning("[BrowserCaptcha] reCAPTCHA 加载超时")
        return False

    async def _execute_recaptcha_on_tab(self, tab) -> Optional[str]:
        """在指定标签页执行 reCAPTCHA 获取 token
        
        Args:
            tab: nodriver 标签页对象
            
        Returns:
            reCAPTCHA token 或 None
        """
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
                        grecaptcha.enterprise.execute('{self.website_key}', {{action: 'FLOW_GENERATION'}})
                            .then(function(token) {{
                                window.{token_var} = token;
                            }})
                            .catch(function(err) {{
                                window.{error_var} = err.message || 'execute failed';
                            }});
                    }});
                }} catch (e) {{
                    window.{error_var} = e.message || 'exception';
                }}
            }})()
        """
        
        # 注入执行脚本
        await tab.evaluate(execute_script)
        
        # 轮询等待结果（最多 15 秒）
        token = None
        for i in range(30):
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

    async def get_token(self, project_id: str, account_id: str = "default") -> Optional[str]:
        """获取 reCAPTCHA token
        
        自动常驻模式：如果该 project_id 没有常驻标签页，则自动创建并常驻
        
        Args:
            project_id: Flow项目ID
            account_id: 账户ID，用于区分不同的浏览器实例和常驻标签页

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
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
                debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] project_id={project_id} 沒有常駐標籤頁，正在創建...")
                resident_info = await self._create_resident_tab(browser, project_id)
                if resident_info is None:
                    debug_logger.log_warning(f"[BrowserCaptcha] 帳號 [{account_id}] 無法為 project_id={project_id} 創建常駐標籤頁，fallback 到傳統模式")
                    return await self._get_token_legacy(browser, project_id, account_id)
                self._account_resident_tabs[account_id][project_id] = resident_info
                debug_logger.log_info(f"[BrowserCaptcha] ✅ 帳號 [{account_id}] 已為 project_id={project_id} 創建常駐標籤頁 (当前共 {len(self._account_resident_tabs[account_id])} 个)")
        
        # 使用常驻标签页生成 token
        if resident_info and resident_info.recaptcha_ready and resident_info.tab:
            start_time = time.time()
            debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] 從常駐標籤頁即時生成 token (project: {project_id})...")
            try:
                token = await self._execute_recaptcha_on_tab(resident_info.tab)
                duration_ms = (time.time() - start_time) * 1000
                if token:
                    debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] ✅ Token生成成功（耗時 {duration_ms:.0f}ms）")
                    return token
                else:
                    debug_logger.log_warning(f"[BrowserCaptcha] 帳號 [{account_id}] 常駐標籤頁生成失敗 (project: {project_id})，嘗試重建...")
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 帳號 [{account_id}] 常駐標籤頁異常: {e}，嘗試重建...")
            
            # 常駐標籤頁失效，嘗試重建
            async with self._resident_lock:
                await self._close_resident_tab(account_id, project_id)
                resident_info = await self._create_resident_tab(browser, project_id)
                if resident_info:
                    self._account_resident_tabs[account_id][project_id] = resident_info
                    # 重建后立即尝试生成
                    try:
                        token = await self._execute_recaptcha_on_tab(resident_info.tab)
                        if token:
                            debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] ✅ 重建後 Token生成成功")
                            return token
                    except Exception:
                        pass
        
        # 最终 Fallback: 使用传统模式
        debug_logger.log_warning(f"[BrowserCaptcha] 帳號 [{account_id}] 所有常駐方式失敗，fallback 到傳統模式 (project: {project_id})")
        return await self._get_token_legacy(browser, project_id, account_id)

    async def _create_resident_tab(self, browser, project_id: str) -> Optional[ResidentTabInfo]:
        """为指定 project_id 创建常驻标签页
        
        Args:
            browser: nodriver 浏览器实例
            project_id: 项目 ID
            
        Returns:
            ResidentTabInfo 对象，或 None（创建失败）
        """
        try:
            website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
            debug_logger.log_info(f"[BrowserCaptcha] 为 project_id={project_id} 创建常驻标签页，访问: {website_url}")
            
            # 创建新标签页
            tab = await browser.get(website_url, new_tab=True)
            
            # 等待页面加载完成
            page_loaded = False
            for retry in range(60):
                try:
                    await asyncio.sleep(1)
                    ready_state = await tab.evaluate("document.readyState")
                    if ready_state == "complete":
                        page_loaded = True
                        break
                except ConnectionRefusedError as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] 标签页连接丢失: {e}")
                    return None
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] 等待页面异常: {e}，重试 {retry + 1}/60...")
                    await asyncio.sleep(1)
            
            if not page_loaded:
                debug_logger.log_error(f"[BrowserCaptcha] 页面加载超时 (project: {project_id})")
                try:
                    await tab.close()
                except:
                    pass
                return None
            
            # 等待 reCAPTCHA 加载
            recaptcha_ready = await self._wait_for_recaptcha(tab)
            
            if not recaptcha_ready:
                debug_logger.log_error(f"[BrowserCaptcha] reCAPTCHA 加载失败 (project: {project_id})")
                try:
                    await tab.close()
                except:
                    pass
                return None
            
            # 创建常驻信息对象
            resident_info = ResidentTabInfo(tab, project_id)
            resident_info.recaptcha_ready = True
            
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 常驻标签页创建成功 (project: {project_id})")
            return resident_info
            
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 创建常驻标签页异常: {e}")
            return None

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

    async def _get_token_legacy(self, browser, project_id: str, account_id: str) -> Optional[str]:
        """传统模式获取 reCAPTCHA token（每次创建新标签页）

        Args:
            project_id: Flow项目ID

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
        start_time = time.time()
        tab = None

        try:
            website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
            debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] [Legacy] 訪問頁面: {website_url}")

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
            recaptcha_ready = await self._wait_for_recaptcha(tab)

            if not recaptcha_ready:
                debug_logger.log_error("[BrowserCaptcha] [Legacy] reCAPTCHA 无法加载")
                return None

            # 执行 reCAPTCHA
            debug_logger.log_info("[BrowserCaptcha] [Legacy] 执行 reCAPTCHA 验证...")
            token = await self._execute_recaptcha_on_tab(tab)

            duration_ms = (time.time() - start_time) * 1000

            if token:
                debug_logger.log_info(f"[BrowserCaptcha] [Legacy] ✅ Token获取成功（耗时 {duration_ms:.0f}ms）")
                return token
            else:
                debug_logger.log_error("[BrowserCaptcha] [Legacy] Token获取失败（返回null）")
                return None

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] [Legacy] 获取token异常: {str(e)}")
            return None
        finally:
            # 关闭标签页（但保留浏览器）
            if tab:
                try:
                    await tab.close()
                except Exception:
                    pass

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

    async def open_login_window(self, account_id: str = "default"):
        """打开登录窗口供用户手动登录 Google"""
        await self.initialize_for_account(account_id)
        browser = self.browser_instances[account_id]
        tab = await browser.get("https://accounts.google.com/")
        debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] 已打開登錄窗口。")

    # ========== Session Token 刷新 ==========

    async def refresh_session_token(self, project_id: str, account_id: str = "default") -> Optional[str]:
        """从常驻标签页获取最新的 Session Token"""
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
                debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] project_id={project_id} 沒有常駐標籤頁，正在創建...")
                resident_info = await self._create_resident_tab(browser, project_id)
                if resident_info is None:
                    debug_logger.log_warning(f"[BrowserCaptcha] 帳號 [{account_id}] 無法為 project_id={project_id} 創建常駐標籤頁")
                    return None
                self._account_resident_tabs[account_id][project_id] = resident_info
        
        if not resident_info or not resident_info.tab:
            debug_logger.log_error(f"[BrowserCaptcha] 无法获取常驻标签页")
            return None
        
        tab = resident_info.tab
        
        try:
            # 刷新页面以获取最新的 cookies
            debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] 刷新常驻标签页以获取最新 cookies...")
            await tab.reload()
            
            # 等待页面加载完成
            for i in range(30):
                await asyncio.sleep(1)
                try:
                    ready_state = await tab.evaluate("document.readyState")
                    if ready_state == "complete":
                        break
                except Exception:
                    pass
            
            # 额外等待确保 cookies 已设置
            await asyncio.sleep(2)
            
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
            
            # 常驻标签页可能已失效，尝试重建
            async with self._resident_lock:
                await self._close_resident_tab(account_id, project_id)
                resident_info = await self._create_resident_tab(browser, project_id)
                if resident_info:
                    if account_id not in self._account_resident_tabs:
                        self._account_resident_tabs[account_id] = {}
                    self._account_resident_tabs[account_id][project_id] = resident_info
                    # 重建后再次尝试获取
                    try:
                        cookies = await browser.cookies.get_all()
                        for cookie in cookies:
                            if cookie.name == "__Secure-next-auth.session-token":
                                debug_logger.log_info(f"[BrowserCaptcha] 帳號 [{account_id}] ✅ 重建後 Session Token 獲取成功")
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