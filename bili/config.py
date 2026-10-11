from typing import Literal

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    config_version: str = Field(default="1.0.0", description="配置结构版本")
    enabled: bool = Field(default=True, description="启用插件")


class AccessConfig(PluginConfigBase):
    admins: list[str] = Field(default_factory=list, description="管理员，格式 platform:user_id，如 qq:12345")
    allow_public_subscribe: bool = Field(default=False, description="允许任何人在当前会话管理订阅")
    allowed_streams: list[str] = Field(default_factory=list, description="允许的聊天流 ID；空列表表示全部")
    max_subscriptions_per_stream: int = Field(default=100, ge=1, le=1000, description="单会话订阅数上限")


class AccountConfig(PluginConfigBase):
    cookie_env: str = Field(default="BILIBILI_COOKIE", description="读取 B 站 Cookie 的环境变量名")
    # Credentials are never accepted in chat commands or printed to logs.
    cookie: str = Field(
        default="", repr=False, description="可选 Cookie；优先使用环境变量，其次此项，再次扫码登录"
    )


class PollingConfig(PluginConfigBase):
    mode: Literal["auto", "feed", "space"] = Field(default="auto", description="auto: 登录后动态流+主页补查")
    feed_interval: float = Field(default=10, ge=10, le=3600, description="关注动态流检查间隔（秒）")
    space_interval: float = Field(default=300, ge=30, le=86400, description="登录时每个 UP 的主页补查间隔")
    anonymous_space_interval: float = Field(
        default=30, ge=10, le=86400, description="未登录时每个 UP 的检查间隔"
    )
    space_tick: float = Field(default=5, ge=1, le=300, description="主页轮询调度间隔，每次检查一个 UP")
    live_interval: float = Field(default=30, ge=15, le=3600, description="批量检查直播间隔")
    season_interval: float = Field(default=120, ge=30, le=86400, description="番剧更新检查间隔")
    max_pages: int = Field(default=5, ge=1, le=30, description="单轮动态分页上限；超限保留游标继续补查")
    catchup_hours: int = Field(default=24, ge=1, le=168, description="中断后补推的最大时间范围")
    request_gap: float = Field(default=1, ge=0.2, le=60, description="API 请求最小间隔，限制全局请求频率")
    timeout: float = Field(default=15, ge=3, le=60, description="单次 HTTP 请求超时（秒）")
    risk_backoff: float = Field(default=120, ge=30, le=3600, description="遇到 412/429/-352 等风控时暂停请求")


class LinkConfig(PluginConfigBase):
    enabled: bool = Field(default=True, description="自动解析聊天中的 B 站链接")
    only_when_mentioned: bool = Field(default=False, description="仅在被提及时解析")
    cooldown: int = Field(default=120, ge=0, le=86400, description="同会话同一内容重复解析冷却秒数")
    max_per_message: int = Field(default=3, ge=1, le=5, description="单条消息最多解析的链接数")
    bare_ids: bool = Field(default=True, description="解析独立的 BV/av/cv/ss/ep/md 号")


class CardConfig(PluginConfigBase):
    renderer: Literal["auto", "host", "pillow", "text"] = Field(
        default="auto", description="auto: Host HTML，失败后 Pillow，再失败发文本"
    )
    width: int = Field(default=880, ge=600, le=1200, description="卡片宽度")
    accent: str = Field(default="#fb7299", pattern=r"^#[0-9a-fA-F]{6}$", description="卡片主题色")
    font_path: str = Field(default="", description="Pillow 中文字体路径；留空自动寻找系统中文字体")
    max_images: int = Field(default=9, ge=0, le=9, description="每张卡片最多显示的图片数")
    max_text: int = Field(default=1600, ge=100, le=4000, description="正文截断字数")
    cache_seconds: int = Field(default=300, ge=0, le=3600, description="卡片缓存时长")


class BiliConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    access: AccessConfig = Field(default_factory=AccessConfig)
    account: AccountConfig = Field(default_factory=AccountConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    links: LinkConfig = Field(default_factory=LinkConfig)
    cards: CardConfig = Field(default_factory=CardConfig)
