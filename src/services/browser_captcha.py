"""
浏览器自动化获取 reCAPTCHA token
使用 Playwright 访问页面并执行 reCAPTCHA 验证
"""
import asyncio
import time
import re
import sys
import os
from typing import Optional, Dict
from playwright.async_api import async_playwright, Browser, BrowserContext

from ..core.logger import debug_logger


def parse_proxy_url(proxy_url: str) -> Optional[Dict[str, str]]:
    """解析代理URL，分离协议、主机、端口、认证信息"""
    proxy_pattern = r'^(socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$'
    match = re.match(proxy_pattern, proxy_url)

    if match:
        protocol, username, password, host, port = match.groups()
        proxy_config = {'server': f'{protocol}://{host}:{port}'}

        if username and password:
            proxy_config['username'] = username
            proxy_config['password'] = password

        return proxy_config
    return None


def validate_browser_proxy_url(proxy_url: str) -> tuple[bool, str]:
    """验证浏览器代理URL格式"""
    if not proxy_url or not proxy_url.strip():
        return True, ""

    proxy_url = proxy_url.strip()
    parsed = parse_proxy_url(proxy_url)

    if not parsed:
        return False, "代理URL格式错误，正确格式：http://host:port 或 socks5://host:port"

    has_auth = 'username' in parsed
    protocol = parsed['server'].split('://')[0]

    if protocol == 'socks5' and has_auth:
        return False, "浏览器不支持带认证的SOCKS5代理，请使用HTTP代理或移除SOCKS5认证"

    if protocol in ['http', 'https']:
        return True, ""

    if protocol == 'socks5' and not has_auth:
        return True, ""

    return False, f"不支持的代理协议：{protocol}"


class BrowserCaptchaService:
    """浏览器自动化获取 reCAPTCHA token（单例模式）"""

    _instance: Optional['BrowserCaptchaService'] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        """初始化服务"""
        self.headless = False  # Now using Xvfb in Docker, so we can use False
        self.playwright = None
        self._initialized = False
        self.contexts: Dict[str, BrowserContext] = {}  # account_id -> BrowserContext
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.db = db
        self._fixed_user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

    def get_user_agent(self, account_id: str = "default") -> str:
        """獲取瀏覽器使用的 User-Agent"""
        return getattr(self, '_fixed_user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

    @classmethod
    async def get_instance(cls, db=None) -> 'BrowserCaptchaService':
        """获取单例实例"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
                    # await cls._instance.initialize()
        return cls._instance

    async def ensure_playwright(self):
        """確保 Playwright 已啟動"""
        if self.playwright is None:
            self.playwright = await async_playwright().start()

    async def initialize_for_account(self, account_id: str):
        """為特定帳號初始化瀏覽器上下文"""
        if account_id in self.contexts:
            return

        await self.ensure_playwright()

        try:
            proxy_url = None
            if self.db:
                captcha_config = await self.db.get_captcha_config()
                if captcha_config.browser_proxy_enabled and captcha_config.browser_proxy_url:
                    proxy_url = captcha_config.browser_proxy_url

            debug_logger.log_info(f"[BrowserCaptcha] 正在啟動瀏覽器... (proxy={proxy_url or 'None'})")
            
            # 診斷：列出目錄內容
            import os
            import sys
            import shutil
            
            # Use relative path for cross-platform compatibility
            user_data_dir = os.path.join(os.getcwd(), 'browser_data', account_id)
            os.makedirs(user_data_dir, exist_ok=True)
            
            sys.stderr.write(f"\n[DIAG] Current Directory: {os.getcwd()}\n")
            sys.stderr.write(f"[DIAG] User Data Dir: {user_data_dir}\n")
            if os.path.exists(user_data_dir):
                sys.stderr.write(f"[DIAG] Files in {user_data_dir}: {os.listdir(user_data_dir)}\n")
            
            # 清理 Chromium 的鎖定文件
            lock_files = ['SingletonLock', 'SingletonSocket', 'SingletonCookie']
            for lf in lock_files:
                p = os.path.join(user_data_dir, lf)
                if os.path.exists(p):
                    try:
                        os.remove(p)
                        sys.stderr.write(f"[DIAG] Removed lock file: {lf}\n")
                    except Exception as e:
                        pass # Ignore errors

            # self.playwright = await async_playwright().start() # Already started in ensure_playwright

            # 定義統一的 User-Agent
            self._fixed_user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            
            # 嘗試從 DB 獲取自定義 UA (例如 Android UA)
            current_ua = self._fixed_user_agent
            if self.db and account_id != "default":
                try:
                    token_info = await self.db.get_token_by_email(account_id)
                    if token_info and token_info.st:
                        import json
                        st_data = json.loads(token_info.st)
                        if isinstance(st_data, dict) and st_data.get("user_agent"):
                            current_ua = st_data.get("user_agent")
                            debug_logger.log_info(f"[BrowserCaptcha] 使用自定義 User-Agent: {current_ua[:50]}...")
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] 獲取 UA 失敗: {e}")
            
            # 更新實例變量以便 get_user_agent 使用 (注意：這里可能會有並發覆蓋問題，但簡單場景下尚可)
            self._fixed_user_agent = current_ua 

            launch_options = {
                # 'user_data_dir' is passed as positional arg 1, do not include in kwargs
                'headless': self.headless,
                'bypass_csp': True, # 繞過 CSP 限制
                'args': [
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-gpu',
                    '--disable-software-rasterizer',
                    '--disable-dbus',
                    '--no-first-run',
                    '--no-default-browser-check',
                    '--remote-debugging-port=0'
                ],
                'viewport': {'width': 1920, 'height': 1080},
                'user_agent': self._fixed_user_agent, # Unified UA
                'locale': 'en-US',
                'timezone_id': 'America/New_York'
            }

            if proxy_url:
                proxy_config = parse_proxy_url(proxy_url)
                if proxy_config:
                    launch_options['proxy'] = proxy_config
                else:
                    debug_logger.log_warning(f"[BrowserCaptcha] 代理URL格式錯誤: {proxy_url}")
            
            try:
                # 使用 launch_persistent_context
                # 注意: user_data_dir 是位置參數 1
                context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir,
                    **launch_options
                )
            except Exception as launch_err:
                sys.stderr.write(f"[CRITICAL] launch_persistent_context FAILED: {launch_err}\n")
                # 嘗試查找系統安裝的 chrome 作為備選
                sys_chrome = shutil.which('google-chrome') or shutil.which('chromium') or shutil.which('chrome.exe')
                if sys_chrome:
                    sys.stderr.write(f"[DIAG] Retrying with explicit path: {sys_chrome}\n")
                    context = await self.playwright.chromium.launch_persistent_context(
                        user_data_dir,
                        executable_path=sys_chrome,
                        **launch_options,
                        viewport={'width': 1920, 'height': 1080},
                        user_agent=self._fixed_user_agent,
                        locale='en-US',
                        timezone_id='America/New_York'
                    )
                else:
                    raise launch_err
            
            # 從 JSON 加載跨平台 Cookie
            import json
            cookies_path = os.path.join(user_data_dir, 'cookies.json')
            if os.path.exists(cookies_path):
                try:
                    with open(cookies_path, 'r') as f:
                        cookies = json.load(f)
                        await context.add_cookies(cookies)
                    debug_logger.log_info(f"[BrowserCaptcha] ✅ 已從 JSON 加載 {len(cookies)} 個 Cookie")
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] ⚠️ 加載 JSON Cookie 失敗: {str(e)}")
            
            sys.stderr.flush()
            
            # 注入 Stealth 脚本
            await context.add_init_script("""
                // 1. 移除 navigator.webdriver
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });

                // 2. 伪造 WebGL 渲染器
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) return 'Intel Inc.';
                    if (parameter === 37446) return 'Intel(R) Iris(TM) Plus Graphics 640';
                    return getParameter.apply(this, arguments);
                };

                // 3. 伪造 chrome 属性
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };

                // 4. 伪造 permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
                );
            """)

            # Add Trusted Types policy to allow creating script URLs
            await context.add_init_script("""
                if (window.trustedTypes && window.trustedTypes.createPolicy) {
                    window.trustedTypes.createPolicy('default', {
                        createHTML: (string, sink) => string,
                        createScript: (string, sink) => string,
                        createScriptURL: (string, sink) => string,
                    });
                }
                
                // CRITICAL: Override platform to match Windows User-Agent
                // Otherwise UA=Windows but Platform=Linux is a dead giveaway
                Object.defineProperty(navigator, 'platform', {
                    get: () => 'Win32'
                });
            """)

            self.contexts[account_id] = context
            self._initialized = True
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 瀏覽器帳號 [{account_id}] 已啟動 (Persistent Context)")
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] ❌ 瀏覽器帳號 [{account_id}] 啟動失敗: {str(e)}")
            raise

    async def get_token(self, project_id: str, account_id: str = "default", st: str = None) -> Optional[str]:
        """获取 reCAPTCHA token"""
        try:
            await self.initialize_for_account(account_id)
            context = self.contexts.get(account_id)
            if not context:
                raise Exception(f"無法獲取帳號 [{account_id}] 的瀏覽器上下文")

            # 注入 Session Token (如果提供)
            if st:
                try:
                    import json
                    debug_logger.log_info(f"[BrowserCaptcha] 正在解析 Session Token...")
                    st_data = json.loads(st)
                    cookie_dict = {}
                    
                    if isinstance(st_data, dict) and "cookies" in st_data:
                        cookie_dict = st_data.get("cookies", {})
                    elif isinstance(st_data, dict):
                        cookie_dict = st_data
                    
                    cookies_to_add = []
                    for name, value in cookie_dict.items():
                        # Domain logic
                        if name.startswith("__Secure-") or name.startswith("__Host-"):
                            domain = "labs.google" # 安全 cookie
                        elif name.startswith("_ga") or name == "SOCS" or name == "AEC":
                            domain = ".google.com" # 通用
                        else:
                            domain = ".labs.google" # 默認

                        cookies_to_add.append({
                            "name": name,
                            "value": value,
                            "domain": domain,
                            "path": "/",
                        })
                    
                    if cookies_to_add:
                        # 先清除舊的可能衝突的 cookies? 不，persistent context 保留比較好。
                        # 但為了確保更新，add_cookies 會覆蓋同名 cookie。
                        await context.add_cookies(cookies_to_add)
                        debug_logger.log_info(f"[BrowserCaptcha] 成功注入 {len(cookies_to_add)} 個 Cookie")
                        
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] Cookie 解析或注入失敗: {e}")

            # 定义访问URL
            website_url = f"https://labs.google/fx/project/{project_id}"
            
            # 嘗試注入 Cookie (通過 DB 查詢 ST)
            if self.db and account_id and account_id != "default":
                try:
                    token_info = await self.db.get_token_by_email(account_id)
                    if token_info and token_info.st:
                        import json
                        debug_logger.log_info(f"[BrowserCaptcha] 為帳號 {account_id} 注入 Cookies...")
                        
                        try:
                            st_data = json.loads(token_info.st)
                            cookie_dict = {}
                            
                            if isinstance(st_data, dict) and "cookies" in st_data:
                                cookie_dict = st_data.get("cookies", {})
                            elif isinstance(st_data, dict):
                                cookie_dict = st_data
                            
                            cookies_to_add = []
                            for name, value in cookie_dict.items():
                                # Domain logic - Google 認證 Cookie 通常在 .google.com
                                if name in ["SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID"]:
                                    domain = ".google.com"
                                elif name.startswith("__Secure-") or name.startswith("__Host-"):
                                     # 其他安全 cookie 嘗試 .google.com，如果不行為 labs.google
                                    domain = ".google.com" 
                                elif name.startswith("_ga") or name == "SOCS" or name == "AEC" or name == "NID":
                                    domain = ".google.com"
                                else:
                                    # 應用層 cookie
                                    domain = ".labs.google"

                                # Set basic attributes
                                cookie_obj = {
                                    "name": name,
                                    "value": value,
                                    "domain": domain,
                                    "path": "/",
                                }

                                # Critical: Enforce flags for Secure cookies to prevent browser rejection
                                if name.startswith("__Secure-") or name.startswith("__Host-"):
                                    cookie_obj["secure"] = True
                                    # cookie_obj["sameSite"] = "None" # Sometimes causing issues if not strict
                                
                                # Google authentication cookies often need secure
                                if name in ["SID", "HSID", "SSID", "APISID", "SAPISID", "OSID"]:
                                    cookie_obj["secure"] = True

                                cookies_to_add.append(cookie_obj)
                            
                            if cookies_to_add:
                                await context.add_cookies(cookies_to_add)
                                debug_logger.log_info(f"[BrowserCaptcha] 成功注入 {len(cookies_to_add)} 個 Cookie")
                        except json.JSONDecodeError:
                            debug_logger.log_warning(f"[BrowserCaptcha] ST 解析失敗 (JSONError)")
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] Cookie 注入過程異常: {e}")

            sys.stderr.write(f"\n[DEBUG] BrowserCaptcha.get_token started for: {website_url}\n")
            sys.stderr.flush()

            start_time = time.time()
            # context = None  <-- This WAS shadow-resetting the context, removed!

            # 使用固定的 User-Agent 以匹配手動登錄會話
            selected_ua = self._fixed_user_agent
            sys.stderr.write(f"[DEBUG] Using Session User-Agent: {selected_ua}\n")

            # 註冊 console 監聽器以讀取注入腳本的 log
            def handle_console(msg):
                # Capture all console logs for debugging
                sys.stderr.write(f"\n[BROWSER_CONSOLE] {msg.type}: {msg.text}\n")
                sys.stderr.flush()

            page = await context.new_page()
            page.on("console", handle_console)

            sys.stderr.write(f"[DEBUG] BrowserCaptcha visiting: {website_url}\n")
            sys.stderr.flush()
            debug_logger.log_info(f"[BrowserCaptcha] 訪問頁面: {website_url}")

            # 访问页面
            try:
                # 预热會話：先訪問 Google 賬號頁面確保會話被識別
                sys.stderr.write("[DEBUG] Warming up session at accounts.google.com...\n")
                try:
                    await page.goto("https://accounts.google.com", wait_until="domcontentloaded", timeout=10000)
                    await asyncio.sleep(1)
                except:
                    pass

                await page.goto(website_url, wait_until="networkidle", timeout=30000)
                
                # 模拟人为交互：滚动和随机移动鼠标
                sys.stderr.write("[DEBUG] Simulating human interaction...\n")
                await page.mouse.move(100, 100)
                await page.mouse.move(500, 400, steps=10)
                await page.evaluate("window.scrollTo(0, 500)")
                await asyncio.sleep(1)
                await page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(0.5)
                sys.stderr.flush()
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 页面加载超时或失败: {str(e)}")

            # 注入攔截腳本以捕獲真實的 action
            await page.add_init_script("""
                window._captured_recaptcha_action = null;
                const originalExecute = window.grecaptcha ? window.grecaptcha.execute : null;
                if (window.grecaptcha) {
                    window.grecaptcha.execute = function(siteKey, options) {
                        if (options && options.action) {
                            window._captured_recaptcha_action = options.action;
                            console.log('[BrowserCaptcha] Captured action:', options.action);
                        }
                        return originalExecute.apply(this, arguments);
                    };
                } else {
                    Object.defineProperty(window, 'grecaptcha', {
                        set: function(val) {
                            window._grecaptcha = val;
                            if (val && val.execute) {
                                const original = val.execute;
                                val.execute = function(siteKey, options) {
                                    if (options && options.action) {
                                        window._captured_recaptcha_action = options.action;
                                        console.log('[BrowserCaptcha] Captured action:', options.action);
                                    }
                                    return original.apply(this, arguments);
                                };
                            }
                        },
                        get: function() { return window._grecaptcha; }
                    });
                }
            """)
            
            # 檢查並注入 reCAPTCHA v3 腳本
            debug_logger.log_info("[BrowserCaptcha] 檢查並加載 reCAPTCHA v3 腳本...")
            script_loaded = await page.evaluate("""
                () => {
                    if (window.grecaptcha && typeof window.grecaptcha.execute === 'function') {
                        return true;
                    }
                    return false;
                }
            """)

            if not script_loaded:
                # 注入脚本
                debug_logger.log_info("[BrowserCaptcha] 使用 add_script_tag 注入 reCAPTCHA v3 腳本...")
                sys.stderr.write(f"[DEBUG] Injecting reCAPTCHA script with key: {self.website_key}\n")
                sys.stderr.flush()
                
                try:
                    await page.add_script_tag(url=f'https://www.google.com/recaptcha/api.js?render={self.website_key}')
                    sys.stderr.write("[DEBUG] Script injection via add_script_tag completed\n")
                except Exception as e:
                    sys.stderr.write(f"[DEBUG] Script injection FAILED: {str(e)}\n")
                sys.stderr.flush()

            # 等待reCAPTCHA加载和初始化
            debug_logger.log_info("[BrowserCaptcha] 等待reCAPTCHA初始化...")
            for i in range(20):
                grecaptcha_ready = await page.evaluate("""
                    () => {
                        return window.grecaptcha &&
                               typeof window.grecaptcha.execute === 'function';
                    }
                """)
                if grecaptcha_ready:
                    debug_logger.log_info(f"[BrowserCaptcha] reCAPTCHA 已准备好")
                    break
                await asyncio.sleep(0.5)
            else:
                debug_logger.log_warning("[BrowserCaptcha] reCAPTCHA 初始化超时")

            # 额外等待确保完全初始化
            sys.stderr.write("[DEBUG] Waiting 5s before execution...\n")
            await page.wait_for_timeout(5000)

            # 执行reCAPTCHA并获取token
            sys.stderr.write("[DEBUG] BrowserCaptcha executing grecaptcha...\n")
            sys.stderr.flush()
            debug_logger.log_info("[BrowserCaptcha] 执行reCAPTCHA验证...")
            
            token = await page.evaluate("""
                async (websiteKey) => {
                    const logs = [];
                    try {
                        logs.push('Step 1: Checking grecaptcha object');
                        logs.push('grecaptcha exists: ' + !!window.grecaptcha);
                        logs.push('grecaptcha.enterprise exists: ' + !!(window.grecaptcha && window.grecaptcha.enterprise));
                        logs.push('grecaptcha.execute exists: ' + !!(window.grecaptcha && typeof window.grecaptcha.execute === 'function'));
                        logs.push('grecaptcha.ready exists: ' + !!(window.grecaptcha && window.grecaptcha.ready));
                        
                        // Check for grecaptcha.enterprise first (for reCAPTCHA Enterprise)
                        if (window.grecaptcha && window.grecaptcha.enterprise) {
                            logs.push('Step 2: Using reCAPTCHA Enterprise');
                            await new Promise((resolve) => {
                                window.grecaptcha.enterprise.ready(() => resolve());
                            });
                            logs.push('Step 3: Enterprise ready, executing...');
                            const token = await window.grecaptcha.enterprise.execute(websiteKey, {
                                action: 'FLOW_GENERATION'
                            });
                            logs.push('Step 4: Enterprise token obtained: ' + (token ? 'Yes' : 'No'));
                            console.log('[BrowserCaptcha] Logs:', logs.join(' | '));
                            return token;
                        }
                        
                        // Fallback to regular grecaptcha v3
                        if (!window.grecaptcha) {
                            logs.push('FAIL: window.grecaptcha does not exist');
                            console.log('[BrowserCaptcha] Logs:', logs.join(' | '));
                            return null;
                        }

                        if (typeof window.grecaptcha.execute !== 'function') {
                            logs.push('FAIL: window.grecaptcha.execute is not a function');
                            logs.push('grecaptcha type: ' + typeof window.grecaptcha);
                            logs.push('grecaptcha keys: ' + Object.keys(window.grecaptcha).join(','));
                            console.log('[BrowserCaptcha] Logs:', logs.join(' | '));
                            return null;
                        }

                        // Wait for grecaptcha ready
                        logs.push('Step 2: Waiting for grecaptcha.ready');
                        await new Promise((resolve, reject) => {
                            const timeout = setTimeout(() => {
                                logs.push('WARNING: grecaptcha.ready timeout');
                                resolve();
                            }, 15000);

                            if (window.grecaptcha && window.grecaptcha.ready) {
                                window.grecaptcha.ready(() => {
                                    clearTimeout(timeout);
                                    logs.push('Step 3: grecaptcha.ready completed');
                                    resolve();
                                });
                            } else {
                                clearTimeout(timeout);
                                logs.push('Step 3: No grecaptcha.ready, proceeding');
                                resolve();
                            }
                        });

                        // Execute reCAPTCHA v3
                        const capturedAction = window._captured_recaptcha_action || 'FLOW_GENERATION';
                        logs.push('Step 4: Executing with action: ' + capturedAction);
                        logs.push('Step 4: Using websiteKey: ' + websiteKey);
                        
                        const token = await window.grecaptcha.execute(websiteKey, {
                            action: capturedAction
                        });
                        
                        logs.push('Step 5: Token obtained: ' + (token ? 'Yes (' + token.substring(0, 20) + '...)' : 'No'));
                        console.log('[BrowserCaptcha] Logs:', logs.join(' | '));
                        return token;
                    } catch (error) {
                        logs.push('EXCEPTION: ' + error.message);
                        logs.push('Stack: ' + error.stack);
                        console.log('[BrowserCaptcha] Logs:', logs.join(' | '));
                        console.error('[BrowserCaptcha] Error:', error);
                        return null;
                    }
                }
            """, self.website_key)

            sys.stderr.write(f"[DEBUG] BrowserCaptcha token obtained: {'Yes' if token else 'No'}\n")
            if token:
                sys.stderr.write(f"[DEBUG] BrowserCaptcha token snippet: {token[:20]}...\n")
            sys.stderr.flush()

            duration_ms = (time.time() - start_time) * 1000

            if token:
                debug_logger.log_info(f"[BrowserCaptcha] ✅ Token获取成功（耗时 {duration_ms:.0f}ms）")
                return token
            else:
                debug_logger.log_error("[BrowserCaptcha] Token获取失败")
                return None

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 內部異常: {str(e)}")
            sys.stderr.write(f"[DEBUG] BrowserCaptcha internal exception: {str(e)}\n")
            sys.stderr.flush()
            return None
        finally:
            # 持久化上下文不在此處關閉，僅關閉頁面
            if 'page' in locals() and page:
                try:
                    await page.close()
                except:
                    pass

    async def close(self):
        """关闭浏览器"""
        try:
            for account_id, context in self.contexts.items():
                try:
                    await context.close()
                except Exception as e:
                    if "Connection closed" not in str(e):
                        debug_logger.log_warning(f"[BrowserCaptcha] 關閉帳號 [{account_id}] 瀏覽器異常: {str(e)}")
            self.contexts.clear()

            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass
                finally:
                    self.playwright = None

            self._initialized = False
            debug_logger.log_info("[BrowserCaptcha] 浏览器已关闭")
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 关闭浏览器异常: {str(e)}")
