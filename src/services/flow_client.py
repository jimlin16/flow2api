"""Flow API Client for VideoFX (Veo)"""
import time
import uuid
import random
import base64
import sys
import asyncio
from typing import Dict, Any, Optional, List
from curl_cffi.requests import AsyncSession
from ..core.logger import debug_logger
from ..core.config import config


class FlowClient:
    """VideoFX API客户端"""

    def __init__(self, proxy_manager, db=None):
        self.proxy_manager = proxy_manager
        self.db = db  # Database instance for captcha config
        self.labs_base_url = config.flow_labs_base_url  # https://labs.google/fx/api
        self.api_base_url = config.flow_api_base_url    # https://aisandbox-pa.googleapis.com/v1
        self.timeout = config.flow_timeout
        # 缓存每个账号的 User-Agent
        self._user_agent_cache = {}
        # [FIX] Initialize browser service reference
        self.browser_service = None

        # [UPSTREAM] Default client headers to mimic Chrome/Android as per upstream flow2api
        self._default_client_headers = {
            "sec-ch-ua-mobile": "?1",
            "sec-ch-ua-platform": "\"Android\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "x-browser-channel": "stable",
            "x-browser-copyright": "Copyright 2026 Google LLC. All Rights reserved.",
            "x-browser-validation": "UujAs0GAwdnCJ9nvrswZ+O+oco0=",
            "x-browser-year": "2026",
            "x-client-data": "CJS2yQEIpLbJAQipncoBCNj9ygEIlKHLAQiFoM0BGP6lzwE="
        }

    async def _generate_user_agent(self, account_id: str = None) -> str:
        """基于账号ID生成固定的 User-Agent
        
        Args:
            account_id: 账号标识（如 email 或 token_id），相同账号返回相同 UA
            
        Returns:
            User-Agent 字符串
        """
        # 如果没有提供账号ID，生成随机UA
        if not account_id:
            account_id = f"random_{random.randint(1, 999999)}"
        
        # [FIX] Force lowercase
        account_id = account_id.lower()

        # 如果已缓存，直接返回
        if account_id in self._user_agent_cache:
            return self._user_agent_cache[account_id]
            
        # [FIX] Re-enable UA sync from browser service.
        # It's CRITICAL that the API Request User-Agent matches the Browser where the reCAPTCHA token was generated.
        if self.browser_service:
            try:
                browser_ua = await self.browser_service.get_user_agent(account_id)
                if browser_ua:
                    self._user_agent_cache[account_id] = browser_ua
                    debug_logger.log_info(f"[DEBUG_UA] Synced UA from Browser: {browser_ua}")
                    return browser_ua
            except Exception as e:
                debug_logger.log_warning(f"Failed to sync UA from browser: {e}")
        
        # 使用账号ID作为随机种子，确保同一账号生成相同的UA
        import hashlib
        seed = int(hashlib.md5(account_id.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        
        # Chrome 版本池
        chrome_versions = ["130.0.0.0", "131.0.0.0", "132.0.0.0", "129.0.0.0"]
        # Firefox 版本池
        firefox_versions = ["133.0", "132.0", "131.0", "134.0"]
        # Safari 版本池
        safari_versions = ["18.2", "18.1", "18.0", "17.6"]
        # Edge 版本池
        edge_versions = ["130.0.0.0", "131.0.0.0", "132.0.0.0"]

        # [FIX] Force match TLS fingerprint (impersonate="chrome110")
        # Using a newer UA (like 130+) with an older TLS fingerprint (110) is a major bot signal.
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
        
        # 缓存结果
        self._user_agent_cache[account_id] = ua
        
        return ua

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        use_st: bool = False,
        st_token: Optional[str] = None,
        use_at: bool = False,
        at_token: Optional[str] = None,
        account_id: Optional[str] = None,
        cookies: Optional[str] = None
    ) -> Dict[str, Any]:
        """统一HTTP请求处理"""
        proxy_url = await self.proxy_manager.get_proxy_url()

        if headers is None:
            headers = {}

        # [FIX] 徹底禁用 API 請求中的 Cookie 以避開 401 衝突。
        # 根據 upstream 標準，身份驗證應僅依賴 Authorization: Bearer Header。
        # if cookies:
        #      headers["Cookie"] = cookies
        # elif use_st and st_token:
        #     headers["Cookie"] = f"__Secure-next-auth.session-token={st_token}"

        # AT认证 - 使用Bearer
        # [FIX] 恢復 Bearer 認證頭。即使存在 Cookie (cookies)，aisandbox API 仍需要 Bearer Token。
        # 此前錯誤地在存在 Cookie 時跳過了 Authorization 頭，導致 401。
        # [FIX] 使用大寫 Authorization 增加相容性。
        if use_at and at_token:
            headers["Authorization"] = f"Bearer {at_token}"

        # 确定账号标识
        # 1. 优先使用传入的 account_id (通常是 email)
        # 2. 其次尝试从 token 中截取 (不推荐，不稳定)
        final_account_id = account_id
        if not final_account_id:
            if st_token:
                final_account_id = st_token[:16]
            elif at_token:
                final_account_id = at_token[:16]

        # 通用请求头
        # First apply default headers
        final_headers = self._default_client_headers.copy()
        # Then update with existing headers
        final_headers.update(headers)
        
        final_headers.update({
            "Content-Type": "application/json",
            "User-Agent": await self._generate_user_agent(final_account_id)
        })
        
        # Use final_headers for the request
        headers = final_headers

        ua = headers.get("User-Agent", "")
        if "Windows" in ua:
             headers["sec-ch-ua-platform"] = '"Windows"'
             headers["sec-ch-ua-mobile"] = "?0"
        elif "Macintosh" in ua:
             headers["sec-ch-ua-platform"] = '"macOS"'
             headers["sec-ch-ua-mobile"] = "?0"
        elif "Linux" in ua and "Android" not in ua:
             headers["sec-ch-ua-platform"] = '"Linux"'
             headers["sec-ch-ua-mobile"] = "?0"
        # Else keep default (Android/?1)

        # [DEBUG] Print critical headers for diagnosis
        cookie_val = headers.get("Cookie", "MISSING")
        masked_cookie = f"{cookie_val[:30]}..." if cookie_val != "MISSING" else "MISSING"
        auth_val = headers.get("Authorization", "MISSING")
        masked_auth = f"{auth_val[:20]}..." if auth_val != "MISSING" else "MISSING"
        
        sys.stderr.write(f"\n[DEBUG_UA] API Request Cookie (Masked): {masked_cookie}\n")
        sys.stderr.write(f"[DEBUG_UA] API Request Authorization (Masked): {masked_auth}\n")
        sys.stderr.write(f"[DEBUG_UA] API Request User-Agent: {headers.get('User-Agent')}\n")
        sys.stderr.write(f"[DEBUG_UA] API Request Platform: {headers.get('sec-ch-ua-platform')}\n")
        sys.stderr.write(f"[DEBUG_UA] API Request Mobile: {headers.get('sec-ch-ua-mobile')}\n")
        sys.stderr.flush()

        # Log request
        if config.debug_enabled:
            debug_logger.log_request(
                method=method,
                url=url,
                headers=headers,
                body=json_data,
                proxy=proxy_url
            )

        start_time = time.time()

        try:
            async with AsyncSession() as session:
                if method.upper() == "GET":
                    response = await session.get(
                        url,
                        headers=headers,
                        proxy=proxy_url,
                        timeout=self.timeout,
                        impersonate="chrome124" # [FIX] Upgrade to chrome124 to match modern UA fingerprints
                    )
                else:  # POST
                    # [FIX] Add Origin and Referer for security/validation
                    headers.setdefault("Origin", "https://labs.google")
                    headers.setdefault("Referer", "https://labs.google/")
                    
                    response = await session.post(
                        url,
                        headers=headers,
                        json=json_data,
                        proxy=proxy_url,
                        timeout=self.timeout,
                        impersonate="chrome124"
                    )

                duration_ms = (time.time() - start_time) * 1000

                # Log response
                if config.debug_enabled:
                    debug_logger.log_response(
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        body=response.text,
                        duration_ms=duration_ms
                    )

                if not (200 <= response.status_code < 300):
                    error_text = response.text
                    sys.stderr.write(f"\n[DEBUG] GOOGLE_API_ERROR Status: {response.status_code}\n")
                    sys.stderr.write(f"[DEBUG] GOOGLE_API_ERROR Body: {error_text}\n")
                    sys.stderr.flush()
                    
                    debug_logger.log_error(
                        error_message=f"Flow API status error: {response.status_code}",
                        status_code=response.status_code,
                        response_text=error_text
                    )
                    raise Exception(f"Flow API request failed: {response.status_code} - {error_text}")

                return response.json()

        except Exception as e:
            error_msg = str(e)
            if config.debug_enabled:
                debug_logger.log_error(error_message=f"Request exception: {error_msg}")
            raise Exception(f"Flow API request failed: {error_msg}")

    # ========== 认证相关 (使用ST) ==========

    async def st_to_at(self, st: str, account_id: Optional[str] = None) -> dict:
        """ST转AT"""
        url = f"{self.labs_base_url}/auth/session"
        result = await self._make_request(
            method="GET",
            url=url,
            use_st=True,
            st_token=st,
            account_id=account_id
        )
        return result

    # ========== 项目管理 (使用ST) ==========

    async def create_project(self, st: str, title: str, account_id: Optional[str] = None) -> str:
        """创建项目,返回project_id"""
        url = f"{self.labs_base_url}/trpc/project.createProject"
        json_data = {
            "json": {
                "projectTitle": title,
                "toolName": "PINHOLE"
            }
        }

        result = await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_st=True,
            st_token=st,
            account_id=account_id
        )

        project_id = result["result"]["data"]["json"]["result"]["projectId"]
        return project_id

    async def delete_project(self, st: str, project_id: str, account_id: Optional[str] = None):
        """删除项目"""
        url = f"{self.labs_base_url}/trpc/project.deleteProject"
        json_data = {
            "json": {
                "projectToDeleteId": project_id
            }
        }

        await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_st=True,
            st_token=st,
            account_id=account_id
        )

    # ========== 余额查询 (使用AT) ==========

    async def get_credits(self, at: str, account_id: Optional[str] = None) -> dict:
        """查询余额"""
        url = f"{self.api_base_url}/credits"
        result = await self._make_request(
            method="GET",
            url=url,
            use_at=True,
            at_token=at,
            account_id=account_id
        )
        return result

    # ========== 图片上传 (使用AT) ==========

    async def upload_image(
        self,
        at: str,
        image_bytes: bytes,
        aspect_ratio: str = "IMAGE_ASPECT_RATIO_LANDSCAPE",
        account_id: Optional[str] = None
    ) -> str:
        """上传图片,返回mediaGenerationId"""
        if aspect_ratio.startswith("VIDEO_"):
            aspect_ratio = aspect_ratio.replace("VIDEO_", "IMAGE_")

        image_base64 = base64.b64encode(image_bytes).decode('utf-8')

        url = f"{self.api_base_url}:uploadUserImage"
        json_data = {
            "imageInput": {
                "rawImageBytes": image_base64,
                "mimeType": "image/jpeg",
                "isUserUploaded": True,
                "aspectRatio": aspect_ratio
            },
            "clientContext": {
                "sessionId": self._generate_session_id(),
                "tool": "ASSET_MANAGER"
            }
        }

        result = await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_at=True,
            at_token=at,
            account_id=account_id
        )

        media_id = result["mediaGenerationId"]["mediaGenerationId"]
        return media_id

    # ========== 图片生成 (使用AT) - 同步返回 ==========

    async def generate_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_name: str,
        aspect_ratio: str,
        image_inputs: Optional[List[Dict]] = None,
        account_id: Optional[str] = None
    ) -> dict:
        """生成图片(同步返回)"""
        url = f"{self.api_base_url}/projects/{project_id}/flowMedia:batchGenerateImages"

        # 使用传入的 account_id，若无则回退到 token 前缀
        final_account_id = account_id if account_id else (at[:16] if at else "default")
        recaptcha_token, cookies = await self._get_recaptcha_token(project_id, account_id=final_account_id, action="IMAGE_GENERATION") or (None, None)
        session_id = self._generate_session_id()

        # [FIX] Match upstream structure: clientContext only at top level, with recaptchaContext object
        json_data = {
            "clientContext": {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE"
            },
            "requests": [{
                "seed": random.randint(1, 99999),
                "imageModelName": model_name,
                "imageAspectRatio": aspect_ratio,
                "prompt": prompt,
                "imageInputs": image_inputs or []
            }]
        }

        result = await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_at=True,
            at_token=at,
            account_id=account_id,
            cookies=cookies
        )

        return result

    # ========== 视频生成 (使用AT) - 异步返回 ==========

    async def generate_video_text(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        account_id: Optional[str] = None
    ) -> dict:
        """文生视频,返回task_id"""
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoText"

        final_account_id = account_id if account_id else (at[:16] if at else "default")
        
        # [FIX] 實施 403/reCAPTCHA 重試邏輯 - 最多重試3次 (參考外部倉庫做法)
        max_retries = 3
        last_error = None
        
        for retry_attempt in range(max_retries):
            sys.stderr.write(f"\n[DEBUG_RETRY] VIDEO_GENERATION attempt {retry_attempt + 1}/{max_retries}\n")
            
            recaptcha_token, cookies = await self._get_recaptcha_token(project_id, account_id=final_account_id, action="VIDEO_GENERATION") or (None, None)
            
            # [DEBUG] Verify token is obtained and not empty
            token_len = len(recaptcha_token) if recaptcha_token else 0
            sys.stderr.write(f"[DEBUG_TOKEN] reCAPTCHA Token Length: {token_len}, First 20 chars: {recaptcha_token[:20] if recaptcha_token else 'EMPTY'}\n")
            
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())

            json_data = {
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                },
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "seed": random.randint(1, 99999),
                    "textInput": {
                        "prompt": prompt
                    },
                    "videoModelKey": model_key,
                    "metadata": {
                        "sceneId": scene_id
                    }
                }]
            }

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at,
                    account_id=final_account_id,
                    cookies=cookies
                )
                return result
            except Exception as e:
                last_error = e
                # 只有在 403 reCAPTCHA 失敗時才重試
                if "403" in str(e) or "reCAPTCHA" in str(e):
                    sys.stderr.write(f"[DEBUG_RETRY] 403 Detected, retrying with fresh token...\n")
                    # 如果有常駐模式，嘗試強制刷新一下頁面
                    if self.browser_service:
                        # [HINT] 可在此處增加強制刷新邏輯
                        pass
                    continue
                else:
                    # 其他錯誤（如 401, 500）直接拋出，不重試
                    raise e
                    
        # 如果重試完仍失敗
        raise last_error

    async def generate_video_start_end(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        end_media_id: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        account_id: Optional[str] = None
    ) -> dict:
        """首尾帧视频生成 (I2V)"""
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoText"

        final_account_id = account_id if account_id else (at[:16] if at else "default")
        
        # [FIX] 實施 403/reCAPTCHA 重試邏輯
        max_retries = 3
        last_error = None
        
        for retry_attempt in range(max_retries):
            sys.stderr.write(f"\n[DEBUG_RETRY] VIDEO_I2V (Start-End) attempt {retry_attempt + 1}/{max_retries}\n")
            recaptcha_token, cookies = await self._get_recaptcha_token(project_id, account_id=final_account_id, action="VIDEO_GENERATION") or (None, None)
            
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())

            json_data = {
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                },
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "seed": random.randint(1, 99999),
                    "textInput": {
                        "prompt": prompt
                    },
                    "videoModelKey": model_key,
                    "videoInputs": [
                        {
                            "imageUsageType": "IMAGE_USAGE_TYPE_START_IMAGE",
                            "mediaId": start_media_id
                        },
                        {
                            "imageUsageType": "IMAGE_USAGE_TYPE_END_IMAGE",
                            "mediaId": end_media_id
                        }
                    ],
                    "metadata": {
                        "sceneId": scene_id
                    }
                }]
            }

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at,
                    account_id=account_id,
                    cookies=cookies
                )
                return result
            except Exception as e:
                last_error = e
                if "403" in str(e) or "reCAPTCHA" in str(e):
                    sys.stderr.write(f"[DEBUG_RETRY] 403 Detected in I2V, retrying...\n")
                    continue
                raise e
        raise last_error

    async def generate_video_start_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        account_id: Optional[str] = None
    ) -> dict:
        """单帧视频生成 (I2V)"""
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoText"

        final_account_id = account_id if account_id else (at[:16] if at else "default")
        
        # [FIX] 實施 403/reCAPTCHA 重試邏輯
        max_retries = 3
        last_error = None
        
        for retry_attempt in range(max_retries):
            sys.stderr.write(f"\n[DEBUG_RETRY] VIDEO_I2V (Start-Image) attempt {retry_attempt + 1}/{max_retries}\n")
            recaptcha_token, cookies = await self._get_recaptcha_token(project_id, account_id=final_account_id, action="VIDEO_GENERATION") or (None, None)
            
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())

            json_data = {
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                },
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "seed": random.randint(1, 99999),
                    "textInput": {
                        "prompt": prompt
                    },
                    "videoModelKey": model_key,
                    "videoInputs": [
                        {
                            "imageUsageType": "IMAGE_USAGE_TYPE_START_IMAGE",
                            "mediaId": start_media_id
                        }
                    ],
                    "metadata": {
                        "sceneId": scene_id
                    }
                }]
            }

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at,
                    account_id=account_id,
                    cookies=cookies
                )
                return result
            except Exception as e:
                last_error = e
                if "403" in str(e) or "reCAPTCHA" in str(e):
                    sys.stderr.write(f"[DEBUG_RETRY] 403 Detected in I2V-Single, retrying...\n")
                    continue
                raise e
        raise last_error

    async def generate_video_reference_images(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        reference_images: List[Dict],
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        account_id: Optional[str] = None
    ) -> dict:
        """参考图视频生成 (R2V)"""
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoText"

        final_account_id = account_id if account_id else (at[:16] if at else "default")
        
        # [FIX] 實施 403/reCAPTCHA 重試邏輯
        max_retries = 3
        last_error = None
        
        for retry_attempt in range(max_retries):
            sys.stderr.write(f"\n[DEBUG_RETRY] VIDEO_R2V attempt {retry_attempt + 1}/{max_retries}\n")
            recaptcha_token, cookies = await self._get_recaptcha_token(project_id, account_id=final_account_id, action="VIDEO_GENERATION") or (None, None)
            
            session_id = self._generate_session_id()
            scene_id = str(uuid.uuid4())

            json_data = {
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                },
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "seed": random.randint(1, 99999),
                    "textInput": {
                        "prompt": prompt
                    },
                    "videoModelKey": model_key,
                    "videoInputs": reference_images,
                    "metadata": {
                        "sceneId": scene_id
                    }
                }]
            }

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at,
                    account_id=account_id,
                    cookies=cookies
                )
                return result
            except Exception as e:
                last_error = e
                if "403" in str(e) or "reCAPTCHA" in str(e):
                    continue
                raise e
        raise last_error

    async def check_video_status(self, at: str, operations: List[Dict], account_id: Optional[str] = None) -> dict:
        """查询视频生成状态"""
        url = f"{self.api_base_url}/video:batchCheckAsyncVideoGenerationStatus"

        json_data = {
            "operations": operations
        }

        result = await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_at=True,
            at_token=at,
            account_id=account_id
        )

        return result

    # ========== 任务相关 ==========
    # ... 其他方法保持不变 (从略,如有需要可查阅) ...

    async def _get_recaptcha_token(self, project_id: str, account_id: str = "default", action: str = "IMAGE_GENERATION", st: Optional[str] = None) -> Optional[tuple[str, Optional[str]]]:
        """从浏览器服务获取reCAPTCHA token和cookies"""
        
        # [DEBUG] Log start of token acquisition
        sys.stderr.write(f"\n[DEBUG] _get_recaptcha_token started for project: {project_id}, account: {account_id}, action: {action}\n")
        sys.stderr.flush()
        debug_logger.log_info(f"[DEBUG] _get_recaptcha_token started for project: {project_id}, account: {account_id}, action: {action}")
        
        try:
            # 如果 st 為空但有 db，嘗試從數據庫獲取最新 ST (用於 Session 注入)
            current_st = st
            if not current_st and self.db:
                try:
                    # [FIX] 修正數據庫方法名稱
                    if "@" in account_id:
                        token_data = await self.db.get_token_by_email(account_id)
                    else:
                        try:
                            token_data = await self.db.get_token(int(account_id))
                        except:
                            token_data = await self.db.get_token_by_email(account_id)
                            
                    if token_data:
                        current_st = token_data.st
                        sys.stderr.write(f"[DEBUG] ST fetched from DB for {account_id}: {'FOUND' if current_st else 'MISSING'}\n")
                    else:
                        sys.stderr.write(f"[DEBUG] No token data found in DB for account: {account_id}\n")
                except Exception as db_e:
                    sys.stderr.write(f"[DEBUG] Fetch ST from DB failed: {db_e}\n")

            from .browser_captcha_personal import BrowserCaptchaService
            if not self.browser_service:
                self.browser_service = await BrowserCaptchaService.get_instance(self.db)

            # [FIX] 增加小量交互延遲，確保頁面「熟成」，提升 reCAPTCHA 評分
            await asyncio.sleep(1.5)
            
            # 获取 token 和 cookies -> (token, cookies)
            # [FIX] 傳遞 st 以備 Session 注入 (防止 401 引起的跳轉)
            result = await self.browser_service.get_token(project_id, account_id, action, st=current_st)

            if result and result[0]:
                token, cookies = result
                sys.stderr.write(f"[DEBUG] _get_recaptcha_token obtained (personal): Yes (Token len={len(token)}, Cookies={'Yes' if cookies else 'No'})\n")
                sys.stderr.flush()
                debug_logger.log_info(f"[DEBUG] _get_recaptcha_token obtained (personal): Yes")
                return token, cookies
            else:
                sys.stderr.write("[DEBUG] _get_recaptcha_token failed (personal): No token returned\n")
                sys.stderr.flush()
                debug_logger.log_error("[DEBUG] _get_recaptcha_token failed (personal): No token returned")
                return None

        except Exception as e:
            sys.stderr.write(f"[ERROR] Failed to get reCAPTCHA token (personal): {e}\n")
            import traceback
            traceback.print_exc()
            sys.stderr.flush()
            debug_logger.log_error(f"Failed to get reCAPTCHA token (personal): {e}")
            return None
        # The original code had an 'elif' here, which is now unreachable.
        # Assuming the intention is to replace the browser/personal captcha logic entirely.
        # If other captcha methods are still needed, they would require a different structure.
        # For now, this block is removed as it was part of the old 'if captcha_method' structure.
        # elif captcha_method in ["yescaptcha", "capmonster", "ezcaptcha", "capsolver"]:
        #      # 这里省略了 _get_api_captcha_token 的实现,因为当前主要调试 browser
        #      pass
        # return None # This return is now handled within the try/except block for the personal service.

    def _generate_session_id(self) -> str:
        return f";{int(time.time() * 1000)}"
