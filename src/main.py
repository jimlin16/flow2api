"""FastAPI application initialization"""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pathlib import Path
import os
import asyncio

from .core.config import config
from .core.database import Database
from .services.flow_client import FlowClient
from .services.proxy_manager import ProxyManager
from .services.token_manager import TokenManager
from .services.load_balancer import LoadBalancer
from .services.concurrency_manager import ConcurrencyManager
from .services.generation_handler import GenerationHandler
from .api import routes, admin


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    print("=" * 60)
    print("Flow2API Starting...")
    print("=" * 60)

    # Get config from setting.toml
    config_dict = config.get_raw_config()

    # Check if database exists (determine if first startup)
    is_first_startup = not db.db_exists()

    # Initialize database tables structure
    await db.init_db()

    # Handle database initialization based on startup type
    if is_first_startup:
        print("[INIT] First startup detected. Initializing database and configuration from setting.toml...")
        await db.init_config_from_toml(config_dict, is_first_startup=True)
        print("[SUCCESS] Database and configuration initialized successfully.")
    else:
        print("[INFO] Existing database detected. Checking for missing tables and columns...")
        await db.check_and_migrate_db(config_dict)
        print("[SUCCESS] Database migration check completed.")

    # Load admin config from database
    admin_config = await db.get_admin_config()
    if admin_config:
        config.set_admin_username_from_db(admin_config.username)
        config.set_admin_password_from_db(admin_config.password)
        config.api_key = admin_config.api_key

    # Load cache configuration from database
    cache_config = await db.get_cache_config()
    config.set_cache_enabled(cache_config.cache_enabled)
    config.set_cache_timeout(cache_config.cache_timeout)
    config.set_cache_base_url(cache_config.cache_base_url or "")

    # Load generation configuration from database
    generation_config = await db.get_generation_config()
    config.set_image_timeout(generation_config.image_timeout)
    config.set_video_timeout(generation_config.video_timeout)

    # Load debug configuration from database
    debug_config = await db.get_debug_config()
    config.set_debug_enabled(debug_config.enabled)

    # Load captcha configuration from database
    captcha_config = await db.get_captcha_config()
    
    config.set_captcha_method(captcha_config.captcha_method)
    config.set_yescaptcha_api_key(captcha_config.yescaptcha_api_key)
    config.set_yescaptcha_base_url(captcha_config.yescaptcha_base_url)
    config.set_capmonster_api_key(captcha_config.capmonster_api_key)
    config.set_capmonster_base_url(captcha_config.capmonster_base_url)
    config.set_ezcaptcha_api_key(captcha_config.ezcaptcha_api_key)
    config.set_ezcaptcha_base_url(captcha_config.ezcaptcha_base_url)
    config.set_capsolver_api_key(captcha_config.capsolver_api_key)
    config.set_capsolver_base_url(captcha_config.capsolver_base_url)

    # Initialize browser captcha service if needed
    browser_service = None
    # [DISABLED] 瀏覽器打碼服務完全停用
    # if captcha_config.captcha_method == "personal":
    #     from .services.browser_captcha_personal import BrowserCaptchaService
    #     browser_service = await BrowserCaptchaService.get_instance(db)
    #     print("[OK] Browser captcha service initialized (nodriver mode)")
        
        # [DISABLED] 瀏覽器自動打碼功能暫時停用 (403 reCAPTCHA 驗證失敗)
        # 待未來整合 NopeCHA 或其他打碼服務後再啟用
        # -------------------------------------------------------------------
        # # [FIX] 启动常驻模式：为所有活躍帳號預載入瀏覽器
        # tokens = await token_manager.get_all_tokens()
        # active_emails = set()
        # for t in tokens:
        #     if t.is_active and t.email:
        #         active_emails.add(t.email.lower())
        # 
        # if not active_emails:
        #     active_emails.add("default")
        # 
        # # [FIX] 服務啟動時先清理所有關聯的 Chrome 進程，確保環境乾淨
        # import psutil
        # browser_data_base = os.path.join(os.getcwd(), "browser_data").lower()
        # print(f"[CLEANUP] Cleaning up old browser processes (base dir: {browser_data_base})...")
        # for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        #     try:
        #         if proc.info['name'] == 'chrome.exe':
        #             cmdline = " ".join(proc.info['cmdline'] or []).lower()
        #             if browser_data_base in cmdline:
        #                 print(f"[WARN] Cleaning up leftover Chrome process (PID: {proc.info['pid']})")
        #                 proc.terminate() # [FIX] Use terminate() for graceful shutdown to save profile data
        #                 try:
        #                     proc.wait(timeout=3)
        #                 except psutil.TimeoutExpired:
        #                     proc.kill() # Force kill if stuck
        #     except (psutil.NoSuchProcess, psutil.AccessDenied):
        #         pass
        # 
        # # 給 Chrome 一點時間完全關閉
        # await asyncio.sleep(1)
        #     
        # # 預先初始化並打開登錄窗口 (改為異步背景執行，避免阻塞服務啟動)
        # print(f"[START] Launching browsers for active accounts: {active_emails}")
        #
        # async def delayed_browser_start(acc_id):
        #     try:
        #         await browser_service.open_login_window(acc_id)
        #         print(f"[OK] [Background] Browser window opened for account: {acc_id}.")
        #     except Exception as e:
        #         print(f"[WARN] [Background] Failed to open login window for {acc_id}: {e}")
        #
        # browser_service = await BrowserCaptchaService.get_instance(db)
        # 
        # # [FIX] 為每個帳號啟動背景任務
        # for idx, email in enumerate(active_emails):
        #     # 為每個帳號延遲不同時間，避免同時啟動 (間隔 1 秒)
        #     async def start_with_delay(acc_id, delay):
        #         await asyncio.sleep(delay)
        #         await delayed_browser_start(acc_id)
        #     asyncio.create_task(start_with_delay(email, idx * 1))
    elif captcha_config.captcha_method == "browser":
        from .services.browser_captcha import BrowserCaptchaService
        browser_service = await BrowserCaptchaService.get_instance(db)
        print("[OK] Browser captcha service initialized (headless mode)")

    # Initialize concurrency manager
    tokens = await token_manager.get_all_tokens()

    await concurrency_manager.initialize(tokens)

    # Start file cache cleanup task
    await generation_handler.file_cache.start_cleanup_task()

    # Start 429 auto-unban task
    async def auto_unban_task():
        """定时任务：每小时检查并解禁429被禁用的token"""
        while True:
            try:
                await asyncio.sleep(3600)  # 每小时执行一次
                await token_manager.auto_unban_429_tokens()
            except Exception as e:
                print(f"[ERROR] Auto-unban task error: {e}")

    auto_unban_task_handle = asyncio.create_task(auto_unban_task())

    # [MODIFIED] 定时任务：每 1 小时同步一次所有 token 的餘額與 ST
    async def periodic_credit_refresh_task():
        """定时任务：每 1 小时更新一次所有活跃 token 的额度，並主動刷新 ST 採樣"""
        while True:
            try:
                # 初始等待 1 分鐘，避免與啟動時的視窗開啟競爭資源
                await asyncio.sleep(60)
                
                # 1. 執行標籤頁保活 (刷新頁面)
                if browser_service:
                    try:
                        await browser_service.keep_alive_all_tabs()
                    except Exception as e:
                        print(f"[WARN] Keep-alive failed: {e}")

                print("[INFO] [Background] Starting hourly credit and ST refresh...")
                
                # 2. 主動刷新 ST (從瀏覽器採樣最新 Cookie)
                try:
                    print("[INFO] [Background] Starting proactive ST refresh sequence...")
                    await token_manager.proactive_refresh_all_st()
                    print("[INFO] [Background] Proactive ST refresh completed.")
                except Exception as e:
                    print(f"[WARN] Proactive ST refresh failed: {e}")

                # 3. 刷新餘額 (輕量級 API 調用)
                tokens = await token_manager.get_all_tokens()
                for t in tokens:
                    if t.is_active:
                        try:
                            await token_manager.refresh_credits(t.id)
                        except Exception as e:
                            print(f"[WARN] Failed to refresh credits for {t.email}: {e}")
                
                # 每 1 小時執行一次
                await asyncio.sleep(3600)
            except Exception as e:
                print(f"[ERROR] Periodic background task error: {e}")
                await asyncio.sleep(60) # 出錯時等待一分鐘再重試

    credit_refresh_task_handle = asyncio.create_task(periodic_credit_refresh_task())

    print(f"[OK] Database initialized")
    print(f"[OK] Total tokens: {len(tokens)}")
    print(f"[OK] Cache: {'Enabled' if config.cache_enabled else 'Disabled'} (timeout: {config.cache_timeout}s)")
    print(f"[OK] File cache cleanup task started")
    print(f"[OK] 429 auto-unban task started (runs every hour)")
    print(f"[OK] Periodic credit refresh task started (runs every 6 hours)")
    print(f"[OK] Server running on http://{config.server_host}:{config.server_port}")
    print("=" * 60)

    yield

    # Shutdown
    print("Flow2API Shutting down...")
    # Stop file cache cleanup task
    await generation_handler.file_cache.stop_cleanup_task()
    # Stop auto-unban task
    auto_unban_task_handle.cancel()
    # Stop credit refresh task
    credit_refresh_task_handle.cancel()
    try:
        await asyncio.gather(auto_unban_task_handle, credit_refresh_task_handle, return_exceptions=True)
    except Exception:
        pass
    # Close browser if initialized
    if browser_service:
        await browser_service.close()
        print("[OK] Browser captcha service closed")
    print("[OK] File cache cleanup task stopped")
    print("[OK] 429 auto-unban task stopped")


# Initialize components
db = Database()
proxy_manager = ProxyManager(db)
flow_client = FlowClient(proxy_manager, db)
token_manager = TokenManager(db, flow_client)
concurrency_manager = ConcurrencyManager()
load_balancer = LoadBalancer(token_manager, concurrency_manager)
generation_handler = GenerationHandler(
    flow_client,
    token_manager,
    load_balancer,
    db,
    concurrency_manager,
    proxy_manager  # 添加 proxy_manager 参数
)

# Set dependencies
routes.set_generation_handler(generation_handler)
admin.set_dependencies(token_manager, proxy_manager, db)

# Create FastAPI app
app = FastAPI(
    title="Flow2API",
    description="OpenAI-compatible API for Google VideoFX (Veo)",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(routes.router)
app.include_router(admin.router)

# Static files - serve tmp directory for cached files
tmp_dir = Path(__file__).parent.parent / "tmp"
tmp_dir.mkdir(exist_ok=True)
app.mount("/tmp", StaticFiles(directory=str(tmp_dir)), name="tmp")

# HTML routes for frontend
static_path = Path(__file__).parent.parent / "static"


@app.get("/", response_class=HTMLResponse)
async def index():
    """Redirect to login page"""
    login_file = static_path / "login.html"
    if login_file.exists():
        return FileResponse(str(login_file))
    return HTMLResponse(content="<h1>Flow2API</h1><p>Frontend not found</p>", status_code=404)


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Login page"""
    login_file = static_path / "login.html"
    if login_file.exists():
        return FileResponse(str(login_file))
    return HTMLResponse(content="<h1>Login Page Not Found</h1>", status_code=404)


@app.get("/manage", response_class=HTMLResponse)
async def manage_page():
    """Management console page"""
    manage_file = static_path / "manage.html"
    if manage_file.exists():
        return FileResponse(str(manage_file))
    return HTMLResponse(content="<h1>Management Page Not Found</h1>", status_code=404)
