"""Flow API Client for VideoFX (Veo)"""
import time
import uuid
import random
import base64
import sys
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

    def _generate_user_agent(self, account_id: str = None) -> str:
        """基于账号ID生成固定的 User-Agent
        
        Args:
            account_id: 账号标识（如 email 或 token_id），相同账号返回相同 UA
            
        Returns:
            User-Agent 字符串
        """
        # 如果没有提供账号ID，生成随机UA
        if not account_id:
            account_id = f"random_{random.randint(1, 999999)}"
        
        # 如果已缓存，直接返回
        if account_id in self._user_agent_cache:
            return self._user_agent_cache[account_id]
        
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

        # 操作系统配置
        os_configs = [
            # Windows
            {
                "platform": "Windows NT 10.0; Win64; x64",
                "browsers": [
                    lambda r: f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{r.choice(chrome_versions)} Safari/537.36",
                    lambda r: f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{r.choice(firefox_versions).split('.')[0]}.0) Gecko/20100101 Firefox/{r.choice(firefox_versions)}",
                    lambda r: f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{r.choice(chrome_versions)} Safari/537.36 Edg/{r.choice(edge_versions)}",
                ]
            },
            # macOS
            {
                "platform": "Macintosh; Intel Mac OS X 10_15_7",
                "browsers": [
                    lambda r: f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{r.choice(chrome_versions)} Safari/537.36",
                    lambda r: f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{r.choice(safari_versions)} Safari/605.1.15",
                    lambda r: f"Mozilla/5.0 (Macintosh; Intel Mac OS X 14.{r.randint(0, 7)}; rv:{r.choice(firefox_versions).split('.')[0]}.0) Gecko/20100101 Firefox/{r.choice(firefox_versions)}",
                ]
            },
            # Linux
            {
                "platform": "X11; Linux x86_64",
                "browsers": [
                    lambda r: f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{r.choice(chrome_versions)} Safari/537.36",
                    lambda r: f"Mozilla/5.0 (X11; Linux x86_64; rv:{r.choice(firefox_versions).split('.')[0]}.0) Gecko/20100101 Firefox/{r.choice(firefox_versions)}",
                    lambda r: f"Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:{r.choice(firefox_versions).split('.')[0]}.0) Gecko/20100101 Firefox/{r.choice(firefox_versions)}",
                ]
            }
        ]

        # 使用固定种子随机选择操作系统和浏览器
        os_config = rng.choice(os_configs)
        browser_generator = rng.choice(os_config["browsers"])
        user_agent = browser_generator(rng)
        
        # 缓存结果
        self._user_agent_cache[account_id] = user_agent
        
        return user_agent

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        use_st: bool = False,
        st_token: Optional[str] = None,
        use_at: bool = False,
        at_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """统一HTTP请求处理"""
        proxy_url = await self.proxy_manager.get_proxy_url()

        if headers is None:
            headers = {}

        # ST认证 - 使用Cookie
        if use_st and st_token:
            headers["Cookie"] = f"__Secure-next-auth.session-token={st_token}"

        # AT认证 - 使用Bearer
        if use_at and at_token:
            headers["authorization"] = f"Bearer {at_token}"

        # 确定账号标识
        account_id = None
        if st_token:
            account_id = st_token[:16]
        elif at_token:
            account_id = at_token[:16]

        # 通用请求头
        headers.update({
            "Content-Type": "application/json",
            "User-Agent": self._generate_user_agent(account_id)
        })

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
                        impersonate="chrome110"
                    )
                else:  # POST
                    response = await session.post(
                        url,
                        headers=headers,
                        json=json_data,
                        proxy=proxy_url,
                        timeout=self.timeout,
                        impersonate="chrome110"
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

    async def st_to_at(self, st: str) -> dict:
        """ST转AT"""
        url = f"{self.labs_base_url}/auth/session"
        result = await self._make_request(
            method="GET",
            url=url,
            use_st=True,
            st_token=st
        )
        return result

    # ========== 项目管理 (使用ST) ==========

    async def create_project(self, st: str, title: str) -> str:
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
            st_token=st
        )

        project_id = result["result"]["data"]["json"]["result"]["projectId"]
        return project_id

    async def delete_project(self, st: str, project_id: str):
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
            st_token=st
        )

    # ========== 余额查询 (使用AT) ==========

    async def get_credits(self, at: str) -> dict:
        """查询余额"""
        url = f"{self.api_base_url}/credits"
        result = await self._make_request(
            method="GET",
            url=url,
            use_at=True,
            at_token=at
        )
        return result

    # ========== 图片上传 (使用AT) ==========

    async def upload_image(
        self,
        at: str,
        image_bytes: bytes,
        aspect_ratio: str = "IMAGE_ASPECT_RATIO_LANDSCAPE"
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
            at_token=at
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
        image_inputs: Optional[List[Dict]] = None
    ) -> dict:
        """生成图片(同步返回)"""
        url = f"{self.api_base_url}/projects/{project_id}/flowMedia:batchGenerateImages"

        account_id = at[:16] if at else "default"
        recaptcha_token = await self._get_recaptcha_token(project_id, account_id) or ""
        session_id = self._generate_session_id()

        request_data = {
            "clientContext": {
                "recaptchaToken": recaptcha_token,
                "projectId": project_id,
                "sessionId": session_id,
                "tool": "PINHOLE"
            },
            "seed": random.randint(1, 99999),
            "imageModelName": model_name,
            "imageAspectRatio": aspect_ratio,
            "prompt": prompt,
            "imageInputs": image_inputs or []
        }

        json_data = {
            "clientContext": {
                "recaptchaToken": recaptcha_token,
                "sessionId": session_id
            },
            "requests": [request_data]
        }

        result = await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_at=True,
            at_token=at
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
        user_paygate_tier: str = "PAYGATE_TIER_ONE"
    ) -> dict:
        """文生视频,返回task_id"""
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoText"

        account_id = at[:16] if at else "default"
        recaptcha_token = await self._get_recaptcha_token(project_id, account_id) or ""
        session_id = self._generate_session_id()
        scene_id = str(uuid.uuid4())

        json_data = {
            "clientContext": {
                "recaptchaToken": recaptcha_token,
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

        result = await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_at=True,
            at_token=at
        )
        return result

    async def generate_video_start_end(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        end_media_id: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE"
    ) -> dict:
        """首尾帧视频生成 (I2V)"""
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoText"

        account_id = at[:16] if at else "default"
        recaptcha_token = await self._get_recaptcha_token(project_id, account_id) or ""
        session_id = self._generate_session_id()
        scene_id = str(uuid.uuid4())

        json_data = {
            "clientContext": {
                "recaptchaToken": recaptcha_token,
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

        result = await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_at=True,
            at_token=at
        )
        return result

    async def generate_video_start_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE"
    ) -> dict:
        """单帧视频生成 (I2V)"""
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoText"

        account_id = at[:16] if at else "default"
        recaptcha_token = await self._get_recaptcha_token(project_id, account_id) or ""
        session_id = self._generate_session_id()
        scene_id = str(uuid.uuid4())

        json_data = {
            "clientContext": {
                "recaptchaToken": recaptcha_token,
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

        result = await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_at=True,
            at_token=at
        )
        return result

    async def generate_video_reference_images(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        reference_images: List[Dict],
        user_paygate_tier: str = "PAYGATE_TIER_ONE"
    ) -> dict:
        """参考图视频生成 (R2V)"""
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoText"

        account_id = at[:16] if at else "default"
        recaptcha_token = await self._get_recaptcha_token(project_id, account_id) or ""
        session_id = self._generate_session_id()
        scene_id = str(uuid.uuid4())

        json_data = {
            "clientContext": {
                "recaptchaToken": recaptcha_token,
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

        result = await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_at=True,
            at_token=at
        )
        return result

    async def check_video_status(self, at: str, operations: List[Dict]) -> dict:
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
            at_token=at
        )

        return result

    # ========== 任务相关 ==========
    # ... 其他方法保持不变 (从略,如有需要可查阅) ...

    async def _get_recaptcha_token(self, project_id: str, account_id: str = "default") -> Optional[str]:
        """获取reCAPTCHA token"""
        sys.stderr.write(f"\n[DEBUG] _get_recaptcha_token started for account: {account_id}, project: {project_id}\n")
        captcha_method = config.captcha_method
        sys.stderr.write(f"[DEBUG] _get_recaptcha_token method: {captcha_method}\n")
        sys.stderr.flush()

        if captcha_method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                token = await service.get_token(project_id, account_id)
                sys.stderr.write(f"[DEBUG] _get_recaptcha_token obtained: {'Yes' if token else 'No'}\n")
                sys.stderr.flush()
                return token
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Browser] error: {str(e)}")
                return None
        elif captcha_method in ["yescaptcha", "capmonster", "ezcaptcha", "capsolver"]:
             # 这里省略了 _get_api_captcha_token 的实现,因为当前主要调试 browser
             pass
        return None

    def _generate_session_id(self) -> str:
        return f";{int(time.time() * 1000)}"
