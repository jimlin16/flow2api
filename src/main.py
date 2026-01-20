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
        print("🎉 First startup detected. Initializing database and configuration from setting.toml...")
        await db.init_config_from_toml(config_dict, is_first_startup=True)
        print("✓ Database and configuration initialized successfully.")
    else:
        print("🔄 Existing database detected. Checking for missing tables and columns...")
        await db.check_and_migrate_db(config_dict)
        print("✓ Database migration check completed.")

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
    if captcha_config.captcha_method == "personal":
        from .services.browser_captcha_personal import BrowserCaptchaService
        browser_service = await BrowserCaptchaService.get_instance(db)
        print("✓ Browser captcha service initialized (nodriver mode)")
        
        # [FIX] 启动常驻模式：为所有活躍帳號預載入瀏覽器
        tokens = await token_manager.get_all_tokens()
        active_emails = set()
        for t in tokens:
            if t.is_active and t.email:
                active_emails.add(t.email.lower())
        
        if not active_emails:
            active_emails.add("default")
        
        # [FIX] 服務重啟時先清理舊的 Chrome 進程
        import psutil
        browser_data_base = os.path.join(os.getcwd(), "browser_data")
        for email in active_emails:
            user_data_dir = os.path.join(browser_data_base, email)
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if proc.info['name'] == 'chrome.exe':
                        cmdline = " ".join(proc.info['cmdline'] or []).lower()
                        if user_data_dir.lower() in cmdline:
                            print(f"⚠ 清理舊的 Chrome 進程 (PID: {proc.info['pid']}, 帳號: {email})")
                            proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        
        # 給 Chrome 一點時間完全關閉
        await asyncio.sleep(1)
            
        # 預先初始化並打開登錄窗口 (改為異步背景執行，避免阻塞服務啟動)
        async def delayed_browser_start(acc_id):
            try:
                # 給予系統一點緩衝時間
                await asyncio.sleep(1)
                await browser_service.open_login_window(acc_id)
                print(f"✓ [Background] Browser window opened for account: {acc_id}.")
            except Exception as e:
                print(f"⚠ [Background] Failed to open login window for {acc_id}: {e}")
        
        # [FIX] 為每個帳號啟動背景任務
        for idx, email in enumerate(active_emails):
            # 為每個帳號延遲不同時間，避免同時啟動
            async def start_with_delay(acc_id, delay):
                await asyncio.sleep(delay)
                await delayed_browser_start(acc_id)
            asyncio.create_task(start_with_delay(email, idx * 2))
    elif captcha_config.captcha_method == "browser":
        from .services.browser_captcha import BrowserCaptchaService
        browser_service = await BrowserCaptchaService.get_instance(db)
        print("✓ Browser captcha service initialized (headless mode)")

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
                print(f"❌ Auto-unban task error: {e}")

    auto_unban_task_handle = asyncio.create_task(auto_unban_task())

    # [NEW] 定时任务：每 6 小时同步一次所有 token 的余额
    async def periodic_credit_refresh_task():
        """定时任务：每 6 小时更新一次所有活跃 token 的额度"""
        while True:
            try:
                # 初始等待 1 分鐘，避免與啟動時的視窗開啟競爭資源
                await asyncio.sleep(60)
                print("🔄 [Background] Starting periodic credit refresh for all tokens...")
                tokens = await token_manager.get_all_tokens()
                for t in tokens:
                    if t.is_active:
                        try:
                            await token_manager.refresh_credits(t.id)
                        except Exception as e:
                            print(f"⚠ Failed to refresh credits for {t.email}: {e}")
                
                # 之後每 6 小時执行一次
                await asyncio.sleep(6 * 3600)
            except Exception as e:
                print(f"❌ Periodic credit refresh task error: {e}")
                await asyncio.sleep(60) # 出錯時等待一分鐘再重試

    credit_refresh_task_handle = asyncio.create_task(periodic_credit_refresh_task())

    print(f"✓ Database initialized")
    print(f"✓ Total tokens: {len(tokens)}")
    print(f"✓ Cache: {'Enabled' if config.cache_enabled else 'Disabled'} (timeout: {config.cache_timeout}s)")
    print(f"✓ File cache cleanup task started")
    print(f"✓ 429 auto-unban task started (runs every hour)")
    print(f"✓ Periodic credit refresh task started (runs every 6 hours)")
    print(f"✓ Server running on http://{config.server_host}:{config.server_port}")
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
        print("✓ Browser captcha service closed")
    print("✓ File cache cleanup task stopped")
    print("✓ 429 auto-unban task stopped")


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
