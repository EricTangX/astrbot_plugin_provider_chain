"""astrbot_plugin_provider_chain 2.1.0 —— 候选链 · ProviderChain。

只保留文本对话模型的故障切换：
- 按 `text_models` 的顺序挑选本次使用的模型（列表留空则跟随 AstrBot 主模型）；
- 调用命中错误关键词（额度耗尽、限流、鉴权失败、模型下线等）时，按顺序往后换；
- 失败模型进入冷却期，冷却结束自动恢复候选资格；
- 切换成功后立即生效，可选落盘到 `agent_runner.config.model.provider_id`。

图像 / 音频的「模型切换」仍然不做；但保留了「先发图再 @」的图片补挂与兜底转码，
解决 QQ 把图和文字拆成两条消息、导致追问看不到图的问题。所有设置都在插件配置表单里。

2.0.0：配置表单按功能拆成「基础 / 切换规则 / 模型与同步 / 图片处理」四张卡片，
枚举项改为下拉选择；内部路径统一由 CFG_PATHS 映射，首次加载会把 1.x 的平铺配置
自动搬迁到分组结构（旧键保留为隐藏字段，可安全回退）。
插件只在内存中挂钩 Provider 层，卸载 / 重载后自动还原，不修改 AstrBot 源码。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.parse
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import LLMResponse, ProviderType
from astrbot.core.provider.manager import ProviderManager
from astrbot.core.provider.provider import Provider

PLUGIN_NAME = "astrbot_plugin_provider_chain"
VERSION = "2.1.0"

# 2.0 起配置按功能分成 4 张卡片（_conf_schema.json 里的 object 组），
# 这里把「逻辑名 → 配置路径」集中一处，避免以后调整结构时又要满地改字符串。
MODE_TEXT = "text"
MODE_IMAGE = "image"
MODE_NAMES = {MODE_TEXT: "文本", MODE_IMAGE: "图像"}
MODE_KEYS = {MODE_TEXT: "models.text_models", MODE_IMAGE: "models.image_models"}
MODE_PATHS = {MODE_TEXT: ("models", "text_models"), MODE_IMAGE: ("models", "image_models")}

CFG_PATHS: dict[str, tuple[str, ...]] = {
    # 基础
    "enabled": ("basic", "enabled"),
    "suppress_text_mention": ("basic", "suppress_text_mention"),
    "warn_when_empty": ("basic", "warn_when_empty"),
    # 切换规则
    "error_keywords": ("rules", "error_keywords"),
    "match_mode": ("rules", "match_mode"),
    "ignore_case": ("rules", "ignore_case"),
    "match_response_text": ("rules", "match_response_text"),
    "max_chain": ("rules", "max_chain"),
    "cooldown_seconds": ("rules", "cooldown_seconds"),
    "failure_policy": ("rules", "failure_policy"),
    "failure_threshold": ("rules", "failure_threshold"),
    # 模型与同步
    "text_models": ("models", "text_models"),
    "image_models": ("models", "image_models"),
    "strategy": ("models", "strategy"),
    "strict_allowlist": ("models", "strict_allowlist"),
    "auto_append_enabled": ("models", "auto_append_enabled"),
    "auto_sync_provider_models": ("models", "auto_sync_provider_models"),
    "auto_sync_interval_seconds": ("models", "auto_sync_interval_seconds"),
    "persist_switch": ("models", "persist_switch"),
    # 图片处理
    "image_wait_followup": ("image", "image_wait_followup"),
    "image_defer_auto_reply": ("image", "image_defer_auto_reply"),
    "image_wait_seconds": ("image", "image_wait_seconds"),
    "image_bridge_ttl": ("image", "image_bridge_ttl"),
    "image_safe_transcode": ("image", "image_safe_transcode"),
    "image_drop_thumbnails": ("image", "image_drop_thumbnails"),
}

# 1.7.x 的平铺键：schema 里仍保留（invisible），首次加载时搬到新路径，
# 既保证升级不丢设置，也让用户能安全回退到 1.7.x。
LEGACY_FLAT_KEYS: tuple[str, ...] = tuple(CFG_PATHS)

# 图片补挂（先发图再 @）用到的常量
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif"}
_MAX_BRIDGE_IMAGES = 3
_MAX_SAFE_IMAGE_BYTES = 16 * 1024 * 1024
_PIL_TRUNCATE_LOCK = threading.Lock()

# 默认关键词：覆盖额度耗尽、限流、鉴权、余额不足、模型不存在等常见错误
# 这些 HTTP 状态码在异常里直接判定为可切换，不写进关键词表。
# 裸数字（401/403/429…）不能再作为子串关键词：正常回复的 repr 里含有
# token 数、请求 ID、时间戳，随便凑出一个 403 就会误判。
_HTTP_ERROR_CODES = frozenset({401, 402, 403, 408, 429, 500, 502, 503, 504})

DEFAULT_KEYWORDS = [
    "Free quota exhausted",
    "AllocationQuota.FreeTierOnly",
    "insufficient_quota",
    "insufficient balance",
    "quota",
    "rate limit",
    "RateLimitError",
    "invalid_api_key",
    "InvalidApiKey",
    "model_not_found",
    "All chat models failed",
    "service unavailable",
    "timed out",
    "APITimeoutError",
    "ReadTimeout",
    "Error code:",
    "HTTP 4",
    "HTTP 5",
]

STRATEGY_HELP = {
    "order": "按列表顺序（默认，推荐）",
    "alphabetical": "按模型 ID 字典序（忽略列表顺序）",
}

# 下拉类字段的合法取值：1.x 是自由文本框，用户可能填了 "demote " 这类带空格的写法，
# 下拉控件认不出来会显示空白，所以启动时统一规范化一次。
ENUM_VALUES: dict[str, set[str]] = {
    "match_mode": {"substring", "regex"},
    "strategy": {"order", "alphabetical"},
    "failure_policy": {"none", "demote", "remove"},
}

# 当前生效的插件实例：挂钩是类级别的，热重载后要能指向新实例
_ACTIVE_PLUGIN = None
# 类级别挂钩是否已就位（跨实例共享，热重载后新实例可直接复用已有挂钩）
_HOOKS_INSTALLED = False

# 抑制正文里的 @：AstrBot 已按 platform_settings.reply_with_mention 自动 @ 提问者，
# 模型再从历史里学一句 "@昵称(QQ号)" 就会显示成 2~3 个重复的 @。
_NO_TEXT_MENTION_MARK = "【回复格式】不要在正文里写 @"
_NO_TEXT_MENTION_RULE = (
    _NO_TEXT_MENTION_MARK
    + "昵称、@昵称(QQ号) 这类提及：平台会自动 @ 提问者，你再写一遍会在客户端显示成"
    "重复的 @。要提到某个人时直接写名字，不要带 @。"
)


def _ok(data: Any = None, message: str | None = None) -> dict:
    return {"status": "ok", "message": message, "data": data if data is not None else {}}


def _error(message: str, data: Any = None) -> dict:
    return {"status": "error", "message": message, "data": data if data is not None else {}}


@register(
    PLUGIN_NAME,
    "local",
    "文本模型调用报错时按优先级自动切换",
    VERSION,
)
class SwitchModelPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        # 保留 AstrBotConfig 本体（它带 save_config 与磁盘路径）；
        # 空配置时 `config or {}` 会丢掉这个引用，导致页面保存无法落盘。
        self.config: dict = config if config is not None else {}

        # (类, 方法名) -> 原始函数
        self._originals: dict[tuple[type, str], Any] = {}
        # 已挂钩的 (类, 方法名) 记录，便于 terminate 还原
        self._patched: list[tuple[type, str, Any]] = []

        self._cooldown_until: dict[str, float] = {}
        self._last_reason: dict[str, str] = {}
        # (模型 ID, 模式) -> 连续失败次数；成功一次即清零
        self._failure_counts: dict[tuple[str, str], int] = {}
        # (模型 ID, 模式) -> 已被 remove 策略移出，本次运行内不再作为候选
        self._self_removed: set[tuple[str, str]] = set()
        # 模式 -> 上次同步过的 AstrBot 侧模型 ID，用于判断用户是否改过
        self._synced_providers: dict[str, str] = {}
        # 后台同步任务（轮询 AstrBot 的主模型 / 图片转述 / 语音模型是否被改）
        self._sync_task: Any = None
        self._switch_count = 0
        self._last_switch: tuple[float, str, str, str] | None = None
        self._current_id: str | None = None
        # 运行时首选模型（仅作用于文本列表）：切换成功后立刻生效，不依赖保存动作
        self._preferred_id: str | None = None
        self._lock = asyncio.Lock()
        self._patched_ready = False
        # ProviderManager.load_provider 的原始实现，用于「装载新模型时补挂钩」
        self._load_provider_orig: Any = None
        # Provider 解析方法的原始实现，用于「切换后立即生效」
        self._resolve_async_orig: Any = None
        self._resolve_sync_orig: Any = None

        # 图片补挂：umo -> (图片引用列表, 过期时间戳)
        self._image_cache: dict[str, tuple[list[str], float]] = {}
        # 本次请求是否带图：umo -> 过期时间戳（解析阶段据此选视觉模型）
        self._image_mode_until: dict[str, float] = {}
        # 拿不到 umo 时的兜底时间戳
        self._image_mode_global: float = 0.0
        # umo -> 最后一次采集过的 message_id：同一条消息只采一次，
        # 否则「事件总线观察器（拿到 CDN URL）」和「on_waiting（拿到本地文件名）」
        # 会把同一张图存成两个不同引用。
        self._image_msg_id: dict[str, str] = {}
        # 纯图片等待追问令牌：umo -> 本轮等待的令牌号
        self._defer_tokens: dict[str, int] = {}
        # 兜底转码结果缓存：引用键 -> (本地 PNG 路径, 过期时间戳)
        self._safe_image_cache: dict[str, tuple[str, float]] = {}
        # EventBus._print_event 的原始实现（旁路观察入站图片）
        self._image_hook_orig: Any = None

        # 「text_models 首行覆盖了 WebUI 主模型」只提示一次，避免刷屏
        self._text_override_warned = False

    # ------------------------------------------------------------ 可视化配置接口


    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        global _ACTIVE_PLUGIN
        # 先做配置结构迁移：必须在任何 _cfg 读取之前完成，
        # 否则 1.x 的平铺值会被分组默认值盖住。
        self._migrate_config_v2()
        self._normalize_enum_values()
        if not self._cfg_bool("enabled", True):
            _ACTIVE_PLUGIN = None
            self.logger.info("[自动切换模型] 插件已禁用（enabled=false），不做任何挂钩。")
            return
        _ACTIVE_PLUGIN = self
        # AstrBot 先加载插件、后加载模型，此刻通常还没有 Provider 子类。
        # 三路兜底：立即试一次 / 每次装载模型后补挂钩 / 全部加载完成后再试一次。
        self._install_manager_hook()
        self._install_resolver_hook()
        self._install_patches()
        self._install_image_observer()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self, *args, **kwargs) -> None:
        """所有模型装载完毕后的最后一道兜底挂钩。"""
        if not self._cfg_bool("enabled", True):
            return
        self._install_patches()
        # 首次同步：把 AstrBot 里当前选中的主模型 / 图片转述模型 / 语音模型提到列表首位
        try:
            self._sync_provider_lists(force=True)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[自动切换模型] 首次同步模型列表失败：%s", exc)
        # 用户之后在 WebUI 里改模型时，后台轮询会接着同步
        self._start_sync_task()
        if not self._patched_ready:
            self.logger.error(
                "[自动切换模型] 加载完成后仍未找到任何 Provider 子类，插件无法生效，"
                "请确认 AstrBot 版本是否修改了 Provider 基类。"
            )

    def _install_manager_hook(self) -> None:
        """包装 ProviderManager.load_provider，模型晚于插件加载时也能挂钩。"""
        if self._load_provider_orig is not None:
            return
        plugin = self

        async def patched_load_provider(manager_self, provider_config):
            active = _ACTIVE_PLUGIN or plugin
            result = await active._load_provider_orig(manager_self, provider_config)
            active._install_patches()
            return result

        self._load_provider_orig = ProviderManager.load_provider
        patched_load_provider._switch_model_hook = True
        ProviderManager.load_provider = patched_load_provider

    def _remove_manager_hook(self) -> None:
        if self._load_provider_orig is not None:
            ProviderManager.load_provider = self._load_provider_orig
            self._load_provider_orig = None

    # ------------------------------------------------ 图片旁路观察（早于所有管线）







    # ------------------------------------------------------------ 图片缓存






    # ------------------------------------------- 解析拦截（切换后立即生效）

    def _install_resolver_hook(self) -> None:
        """接管 Provider 解析：让本轮选中的模型立刻生效，无需保存或重启。"""
        if self._resolve_async_orig is not None:
            return
        plugin = self

        async def patched_async(manager_self, *args, **kwargs):
            active = _ACTIVE_PLUGIN or plugin
            resolved = await active._resolve_async_orig(manager_self, *args, **kwargs)
            return active._apply_preferred(
                kwargs.get("provider_type", args[0] if args else None),
                resolved,
                kwargs.get("umo", args[1] if len(args) > 1 else None),
            )

        def patched_sync(manager_self, *args, **kwargs):
            active = _ACTIVE_PLUGIN or plugin
            resolved = active._resolve_sync_orig(manager_self, *args, **kwargs)
            return active._apply_preferred(
                kwargs.get("provider_type", args[0] if args else None),
                resolved,
                kwargs.get("umo", args[1] if len(args) > 1 else None),
            )

        patched_async._switch_model_hook = True
        patched_sync._switch_model_hook = True
        self._resolve_async_orig = ProviderManager.get_using_provider_async
        self._resolve_sync_orig = ProviderManager.get_using_provider
        ProviderManager.get_using_provider_async = patched_async
        ProviderManager.get_using_provider = patched_sync
        self.logger.info("[自动切换模型] 已接管 Provider 解析：切换与按列表选模均即时生效。")

    def _remove_resolver_hook(self) -> None:
        if self._resolve_async_orig is not None:
            ProviderManager.get_using_provider_async = self._resolve_async_orig
            self._resolve_async_orig = None
        if self._resolve_sync_orig is not None:
            ProviderManager.get_using_provider = self._resolve_sync_orig
            self._resolve_sync_orig = None

    def _config_provider_id(self) -> str | None:
        try:
            cfg = self.context.get_config()
            return (
                cfg.get("agent_runner", {})
                .get("config", {})
                .get("model", {})
                .get("provider_id")
            )
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------- AstrBot 模型设置 → 列表同步

    @staticmethod
    def _dig(cfg: Any, path: tuple[str, ...]) -> str:
        """按路径读字符串配置，取不到返回空串。"""
        node = cfg
        for part in path:
            if not isinstance(node, dict):
                return ""
            node = node.get(part)
        return str(node).strip() if isinstance(node, str) else ""

    def _astrbot_mode_providers(self) -> dict[str, str]:
        """AstrBot 当前配置的文本对话模型。"""
        cfg = self.context.get_config()
        return {
            MODE_TEXT: self._dig(
                cfg, ("agent_runner", "config", "model", "provider_id")
            )
        }

    def _sync_provider_lists(self, force: bool = False) -> list[str]:
        """把用户在 AstrBot 里新选的模型提到对应列表首位。

        规则：
        - 只在列表非空时生效——空列表本就代表「跟随 AstrBot 默认」，天然就是首选；
        - 已在首位则什么都不做，避免无意义落盘；
        - 不在列表里就插到首位（纳入 + 置顶 + 重新排序一步完成）；
        - 选中的不是对话模型（STT / TTS / embedding 等）时跳过并记日志。

        返回被改动的配置键，方便日志排查与单测断言。
        """
        if not self._cfg_bool("auto_sync_provider_models", True):
            return []
        wanted = self._astrbot_mode_providers()
        if not wanted:
            return []

        instances = self._chat_instances()
        changed: list[str] = []
        strict = self._strict_allowlist()

        for mode, pid in wanted.items():
            if not pid:
                continue
            if not force and self._synced_providers.get(mode) == pid:
                continue
            self._synced_providers[mode] = pid

            key = MODE_KEYS[mode]
            path = MODE_PATHS[mode]
            items = self._as_id_list(self._get_path(path))
            if not items or items[0] == pid:
                continue
            if strict and pid not in items:
                # 严格候选集：不把 AstrBot 里新选的主模型塞进非空列表，
                # 否则「候选列表」会被自动扩充，出现列表外的模型调用。
                self.logger.warning(
                    "[自动切换模型] AstrBot 的%s模型是 %s，但它不在 %s 里；"
                    "严格候选集开启，不自动加入。需要它请手动加进列表。",
                    MODE_NAMES[mode],
                    pid,
                    key,
                )
                continue
            if not self._is_chat_instance(instances.get(pid)):
                self.logger.info(
                    "[自动切换模型] 检测到 AstrBot 的%s模型改为 %s，"
                    "但它不是对话模型，跳过同步到 %s。",
                    MODE_NAMES[mode],
                    pid,
                    key,
                )
                continue

            items = [item for item in items if item != pid]
            items.insert(0, pid)
            self._set_path(path, items)
            # 用户主动选了它，解除此前因连续失败被移出的状态
            self._self_removed.discard((pid, mode))
            changed.append(key)
            self.logger.info(
                "[自动切换模型] AstrBot 的%s模型已换为 %s，同步到 %s 首位 → [%s]",
                MODE_NAMES[mode],
                pid,
                key,
                "、".join(items),
            )

        if changed:
            self._save_config_quietly("同步 AstrBot 模型设置")
        return changed

    def _start_sync_task(self) -> None:
        """启动后台轮询（幂等：已在跑就不重复起）。"""
        task = self._sync_task
        if task is not None and not getattr(task, "done", lambda: True)():
            return
        try:
            self._sync_task = asyncio.create_task(self._sync_loop())
        except RuntimeError as exc:  # 没有事件循环时不应该发生，兜底
            self._sync_task = None
            self.logger.warning("[自动切换模型] 启动模型同步任务失败：%s", exc)

    async def _sync_loop(self) -> None:
        try:
            while True:
                # 每轮重读，改配置后不用重启也能调整节奏
                interval = max(
                    5, min(self._cfg_int("auto_sync_interval_seconds", 20), 600)
                )
                await asyncio.sleep(interval)
                try:
                    self._sync_provider_lists()
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("[自动切换模型] 模型同步出错：%s", exc)
        except asyncio.CancelledError:
            raise

    async def _stop_sync_task(self) -> None:
        task = self._sync_task
        self._sync_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("[自动切换模型] 同步任务退出时抛出异常：%s", exc)

    def _best_available(self, mode: str = MODE_TEXT, allow_cooldown: bool = False):
        """按候选顺序取一个本次可用的模型。

        `allow_cooldown=True` 用于严格候选集：列表内模型即使全在冷却中，也宁可重试它，
        而不是退回到列表外的主模型——越界比多试一次更糟。
        """
        candidates = self._candidates(set(), mode)
        if not candidates:
            if not allow_cooldown:
                return None
            instances = self._chat_instances()
            for pid in self._ordered_ids(mode):
                inst = instances.get(pid)
                if inst is not None:
                    return inst
            return None
        if mode == MODE_TEXT and self._preferred_id:
            for pid, inst in candidates:
                if pid == self._preferred_id:
                    return inst
        return candidates[0][1]

    # --------------------------------------------------------- 严格候选集

    def _strict_allowlist(self) -> bool:
        """严格候选集开关：开且列表非空时，只允许调用列表内的模型。"""
        return self._cfg_bool("strict_allowlist", True)

    def _allowed_ids(self, mode: str) -> set[str]:
        """本次该模式下被允许调用的模型集合；空集表示不做限制。"""
        if not self._strict_allowlist():
            return set()
        return set(self._list_of(mode))

    def _pid_allowed(self, pid: str, mode: str) -> bool:
        allowed = self._allowed_ids(mode)
        return not allowed or pid in allowed

    def _mark_image_request(self, umo: str) -> None:
        """登记「本会话这次请求带图」，解析阶段据此改走视觉模型。

        为什么要单独记：AstrBot 的图片转述只处理**当前事件里**的图，插件补挂的图
        发生在转述之后，永远不会被转述；只有把请求真正交给支持 image 的模型，
        这些图才会被看到。
        """
        ttl = max(30, self._cfg_int("image_bridge_ttl", 120))
        until = time.time() + ttl
        self._image_mode_until[umo] = until
        self._image_mode_global = until

    def _is_image_request(self, umo: str | None = None) -> bool:
        """本会话这次请求是否带图。

        有 umo 时只认本会话的登记：全局时间戳只作为「拿不到 umo」时的兜底，
        否则一次发图会让之后两分钟内**所有会话**的文本请求都跑去视觉模型。
        """
        now = time.time()
        if umo:
            return self._image_mode_until.get(umo, 0.0) > now
        return self._image_mode_global > now

    def _apply_preferred(self, provider_type, resolved, umo: str | None = None):
        """解析拦截：带图请求走视觉模型，其余按文本列表选模。"""
        value = getattr(provider_type, "value", str(provider_type))
        if value != ProviderType.CHAT_COMPLETION.value:
            return resolved

        if self._is_image_request(umo):
            target = self._best_available(MODE_IMAGE, allow_cooldown=True)
            if target is not None:
                if target is not resolved:
                    self.logger.info(
                        "[自动切换模型] 本次是图像请求，按 image_models 改用 %s",
                        self._pid(target),
                    )
                return target
            self.logger.warning(
                "[自动切换模型] 本次是图像请求，但 image_models 为空或没有可用模型，"
                "沿用 %s（图片可能被丢弃）。",
                self._pid(resolved) if resolved is not None else "None",
            )
            return resolved

        text_list = self._list_of(MODE_TEXT)
        strict = self._strict_allowlist() and bool(text_list)
        if not text_list and not self._preferred_id:
            # 文本列表留空且从未切换过 → 完全沿用 AstrBot 解析出的主模型
            return resolved

        if self._preferred_id:
            # 配置里的主模型被人为改成了别的模型 → 尊重配置，放弃运行时偏好
            cfg_pid = self._config_provider_id()
            if (
                self._cfg_bool("persist_switch", True)
                and cfg_pid
                and cfg_pid != self._preferred_id
            ):
                self.logger.info(
                    "[自动切换模型] 检测到主模型配置已变为 %s，放弃运行时偏好 %s。",
                    cfg_pid,
                    self._preferred_id,
                )
                self._preferred_id = None
                if not strict:
                    return resolved
                # 严格模式：偏好失效不等于可以越界，继续按列表选模

        best = self._best_available(MODE_TEXT, allow_cooldown=True)
        if best is None:
            if strict:
                # 列表里的模型一个都没启用：交给调用点拦截，绝不退回列表外的模型
                self.logger.error(
                    "[自动切换模型] text_models 里没有任何已启用的模型，本次文本请求将被拦截（严格候选集）。"
                )
            return resolved
        if best is resolved:
            return resolved

        # 首次发现 text_models 首行覆盖了 WebUI 主模型时提示一次，避免每条消息刷日志
        if not self._text_override_warned and text_list and not self._preferred_id:
            cfg_pid = self._config_provider_id()
            if cfg_pid and cfg_pid != self._pid(best):
                self._text_override_warned = True
                self.logger.info(
                    "[自动切换模型] 文本请求实际使用 %s（依据 text_models）；"
                    "WebUI 主模型 %s 不参与。若想让主模型生效，把 text_models 留空即可。",
                    self._pid(best),
                    cfg_pid,
                )

        self.logger.debug(
            "[自动切换模型] 解析拦截：本应使用 %s，改用 %s",
            self._pid(resolved) if resolved is not None else "None",
            self._pid(best),
        )
        return best

    async def terminate(self) -> None:
        global _ACTIVE_PLUGIN
        if _ACTIVE_PLUGIN is self:
            _ACTIVE_PLUGIN = None
        await self._stop_sync_task()
        self._remove_patches()
        self._remove_resolver_hook()
        self._remove_manager_hook()
        self._remove_image_observer()
        self._image_cache.clear()
        self._defer_tokens.clear()
        self._safe_image_cache.clear()
        self.logger.info("[自动切换模型] 已卸载，Provider 钩子已还原。")

    # ------------------------------------------------------------------ 配置读取

    def _get_path(self, path: tuple[str, ...], default: Any = None) -> Any:
        """按分组路径读值，缺任何一层都返回 default。"""
        node: Any = self.config
        for part in path:
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    def _set_path(self, path: tuple[str, ...], value: Any) -> None:
        """按分组路径写值，中间层缺失时自动补空字典。"""
        node: Any = self.config
        for part in path[:-1]:
            nxt = node.get(part) if isinstance(node, dict) else None
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[path[-1]] = value

    def _cfg(self, key: str, default: Any = None) -> Any:
        """读配置：优先分组路径，回退 1.x 平铺键（未迁移或用户降级时仍可用）。"""
        path = CFG_PATHS.get(key)
        value = self._get_path(path, None) if path else None
        if value is None:
            value = self.config.get(key, None)
        return default if value is None else value

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是"}
        return bool(value)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _normalize_enum_values(self) -> None:
        """把下拉字段的取值改成标准写法（去空格、转小写）。

        1.x 用文本框时可能存成 "demote "、"Order" 这类形式，运行期虽然能容错，
        但下拉控件匹配不到选项会显示空白。这里统一收敛，只在不一致时落盘。
        """
        fixed: list[str] = []
        for key, allowed in ENUM_VALUES.items():
            raw = self._cfg(key, None)
            if not isinstance(raw, str):
                continue
            value = raw.strip().lower()
            if value not in allowed or value == raw:
                continue
            self._set_path(CFG_PATHS[key], value)
            fixed.append("%s: %r → %r" % (key, raw, value))

        if not fixed:
            return
        self._save_config_quietly("枚举值规范化")
        self.logger.info(
            "[自动切换模型] 已规范化下拉字段取值：%s", "；".join(fixed)
        )

    def _migrate_config_v2(self) -> None:
        """把 1.x 的平铺配置搬进 2.0 的分组结构，只跑一次。

        AstrBot 在构造 AstrBotConfig 时会删掉 schema 里没有的键，所以旧键
        以 `invisible` 的形式保留在 schema 里；这里把它们逐项复制到新路径，
        并打上 `_migrated_v2` 标记，之后用户在新表单里的修改不会被覆盖。
        """
        if self._get_path(("_migrated_v2",), False):
            return

        # 先保证四个分组容器存在：后面无论是否有旧值，写入都不会踩空。
        for group in ("basic", "rules", "models", "image"):
            if not isinstance(self.config.get(group), dict):
                self.config[group] = {}

        moved: list[str] = []
        for key in LEGACY_FLAT_KEYS:
            if key not in self.config:
                continue
            legacy = self.config.get(key)
            if legacy is None:
                continue
            path = CFG_PATHS[key]
            self._set_path(path, legacy)
            moved.append(key)

        self._set_path(("_migrated_v2",), True)
        saved = self._save_config_quietly("v2 配置迁移")
        self.logger.info(
            "[自动切换模型] 配置结构升级到 2.0：已迁移 %d 项到分组结构%s。",
            len(moved),
            "（已落盘）" if saved else "（仅内存，配置对象不支持落盘）",
        )

    def _keywords(self) -> list[str]:
        raw = self._cfg("error_keywords")
        if isinstance(raw, str):
            raw = [line for line in re.split(r"[\n,，;；]", raw)]
        if not isinstance(raw, list) or not raw:
            raw = DEFAULT_KEYWORDS
        result = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                result.append(item.strip())
        return result or DEFAULT_KEYWORDS

    def _strategy(self) -> str:
        strategy = str(self._cfg("strategy", "order")).strip().lower()
        return strategy if strategy in STRATEGY_HELP else "order"

    @staticmethod
    def _as_id_list(raw: Any) -> list[str]:
        if isinstance(raw, str):
            raw = [line for line in re.split(r"[\n,，;；]", raw)]
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if isinstance(x, str) and x.strip()]

    def _list_of(self, mode: str = MODE_TEXT) -> list[str]:
        """取某一类模型的优先级列表（文本 / 图像）。"""
        if mode == MODE_IMAGE:
            return self._as_id_list(self._cfg("image_models"))
        items = self._as_id_list(self._cfg("text_models"))
        if not items:
            # 兼容 1.0.0 的配置：老的 models 列表就是文本列表
            items = self._as_id_list(self.config.get("models"))
        return items

    # ------------------------------------------------ 可选模型集合（供页面下拉使用）




    @staticmethod
    def _empty_text_warning(rows: dict[str, list[str]]) -> str:
        if not any(rows.values()):
            return "三份列表都为空：插件不会改变任何模型选择，全部交回 AstrBot 原逻辑。"
        return ""





    # ---------------------------------------------------------------- 请求分类









    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any = None, *args, **kwargs) -> None:
        """请求发出前的最后一道加工：抑制正文 @、补挂先到的图片。"""
        if req is None or not self._cfg_bool("enabled", True):
            return
        self._inject_no_text_mention(req)
        self._bridge_cached_images(event, req)
        await self._prepare_images_for_model(req)

    def _inject_no_text_mention(self, req: Any) -> None:
        """告诉模型别在正文里写 @——平台已经自动 @ 了，重复写会显示成多个 @。"""
        if not self._cfg_bool("suppress_text_mention", True):
            return
        current = getattr(req, "system_prompt", "") or ""
        if not isinstance(current, str):
            return
        if _NO_TEXT_MENTION_MARK in current:
            return  # 工具循环里本钩子可能被多次触发，避免重复追加
        current = current.rstrip()
        req.system_prompt = (
            current + "\n\n" + _NO_TEXT_MENTION_RULE if current else _NO_TEXT_MENTION_RULE
        )
        self.logger.debug("[自动切换模型] 已在系统提示词中追加「正文不要写 @」约束")


    # --------------------------------------------------------- 图片兜底转码












    # ------------------------------------------------------------------ 挂钩实现

    @staticmethod
    def _provider_classes() -> list[type]:
        """递归收集 Provider 的全部子类（含未来新增的 source 实现）。"""
        classes: list[type] = []
        stack: list[type] = [Provider]
        seen: set[type] = set()
        while stack:
            cls = stack.pop()
            for sub in cls.__subclasses__():
                if sub in seen:
                    continue
                seen.add(sub)
                classes.append(sub)
                stack.append(sub)
        return classes

    def _install_patches(self) -> None:
        global _HOOKS_INSTALLED
        count = 0
        for cls in self._provider_classes():
            for attr in ("text_chat", "text_chat_stream"):
                fn = cls.__dict__.get(attr)
                if fn is None:
                    continue
                if getattr(fn, "_switch_model_hook", False):
                    # 已由其它（或上一次加载的）实例挂钩，可直接复用
                    _HOOKS_INSTALLED = True
                    continue
                wrapper = self._make_wrapper(fn, attr == "text_chat_stream")
                setattr(wrapper, "_switch_model_hook", True)
                setattr(wrapper, "__wrapped__", fn)
                setattr(cls, attr, wrapper)
                self._originals[(cls, attr)] = fn
                self._patched.append((cls, attr, fn))
                count += 1
        self._patched_ready = bool(self._patched) or _HOOKS_INSTALLED
        if count == 0:
            return
        _HOOKS_INSTALLED = True
        self.logger.info(
            "[自动切换模型] 已挂钩 %s 个 Provider 方法（%s 个类）。文本候选 %s 个，关键词 %s 条。",
            len(self._patched),
            len({c.__name__ for c, _, _ in self._patched}),
            len(self._list_of(MODE_TEXT)),
            len(self._keywords()),
        )

    def _remove_patches(self) -> None:
        global _HOOKS_INSTALLED
        for cls, attr, orig in self._patched:
            try:
                setattr(cls, attr, orig)
            except Exception:  # noqa: BLE001
                self.logger.warning("[自动切换模型] 还原 %s.%s 失败", cls.__name__, attr)
        self._patched.clear()
        self._originals.clear()
        self._patched_ready = False
        _HOOKS_INSTALLED = False

    def _make_wrapper(self, orig: Any, is_stream: bool):
        plugin = self

        if is_stream:
            async def wrapper(self, *args, **kwargs):
                active = _ACTIVE_PLUGIN or plugin
                async for chunk in active._stream_with_switch(self, orig, args, kwargs):
                    yield chunk
        else:
            async def wrapper(self, *args, **kwargs):
                active = _ACTIVE_PLUGIN or plugin
                return await active._chat_with_switch(self, orig, args, kwargs)

        return wrapper

    def _original_for(self, provider: Any, attr: str):
        """取候选 Provider 自己的原始实现，避免跨类调用错方法或调回包装器。"""
        for cls in type(provider).__mro__:
            original = self._originals.get((cls, attr))
            if original is not None:
                return original
        # 热重载后本实例没有记录：沿 MRO 找定义处，若是包装器则取 __wrapped__
        for cls in type(provider).__mro__:
            fn = cls.__dict__.get(attr)
            if fn is not None:
                return getattr(fn, "__wrapped__", fn)
        return getattr(provider, attr)

    @staticmethod
    def _pid(provider: Any) -> str:
        try:
            return str(provider.provider_config.get("id") or type(provider).__name__)
        except Exception:  # noqa: BLE001
            return type(provider).__name__

    # ------------------------------------------------------------------ 错误判定

    def _match(self, target: Any) -> tuple[bool, str]:
        """判断错误是否命中配置关键词，返回 (是否命中, 命中片段)。"""
        keywords = self._keywords()
        ignore_case = self._cfg_bool("ignore_case", True)
        use_regex = str(self._cfg("match_mode", "substring")).lower() == "regex"

        texts = [str(target)]
        if isinstance(target, BaseException):
            texts.append(type(target).__name__)
            body = getattr(target, "body", None)
            if body:
                try:
                    texts.append(json.dumps(body, ensure_ascii=False, default=str))
                except Exception:  # noqa: BLE001
                    texts.append(str(body))
            response = getattr(target, "response", None)
            status = getattr(response, "status_code", None)
            if status is not None:
                try:
                    code = int(status)
                except (TypeError, ValueError):
                    code = 0
                if code in _HTTP_ERROR_CODES:
                    return True, "HTTP %s" % code
                texts.append(str(status))

        blob = "\n".join(t for t in texts if t)
        haystack = blob.lower() if ignore_case else blob

        for keyword in keywords:
            if use_regex:
                flags = re.IGNORECASE if ignore_case else 0
                try:
                    if re.search(keyword, blob, flags):
                        return True, keyword
                except re.error:
                    continue
            else:
                needle = keyword.lower() if ignore_case else keyword
                if needle in haystack:
                    return True, keyword
        return False, ""

    @staticmethod
    def _is_err_response(resp: Any) -> bool:
        return str(getattr(resp, "role", "") or "").lower() == "err"

    @staticmethod
    def _text_of(resp: Any) -> str:
        try:
            return str(getattr(resp, "completion_text", "") or "")
        except Exception:  # noqa: BLE001
            return ""

    def _should_switch(self, target: Any) -> tuple[bool, str]:
        """这次返回/异常是否该触发切换。

        只有「异常」才拿整个对象做匹配（里面才有 status_code、body 这些真凭据）；
        正常响应只比对 completion_text —— 否则 str(LLMResponse) 里的 token 数、
        请求 ID、时间戳都可能凑出 403/401 这类裸数字，造成误判。
        """
        if isinstance(target, BaseException):
            hit, fragment = self._match(target)
            return (True, fragment) if hit else (False, "")

        if self._is_err_response(target):
            # 错误响应：正文就是错误信息，一律参与匹配
            hit, fragment = self._match(self._text_of(target))
            return (True, fragment) if hit else (False, "")

        if self._cfg_bool("match_response_text", True):
            hit, fragment = self._match(self._text_of(target))
            if hit:
                return True, fragment
        return False, ""

    # ------------------------------------------------------------------ 候选与冷却

    def _manager(self):
        return getattr(self.context, "provider_manager", None)

    def _chat_instances(self) -> dict[str, Any]:
        """只保留对话模型：排除 TTS / STT / embedding / rerank 等其它类型。"""
        manager = self._manager()
        if manager is None:
            return {}
        result: dict[str, Any] = {}
        for inst in getattr(manager, "provider_insts", []) or []:
            if not self._is_chat_instance(inst):
                continue
            result[self._pid(inst)] = inst
        for pid, inst in (getattr(manager, "inst_map", {}) or {}).items():
            if pid in result or not self._is_chat_instance(inst):
                continue
            result[str(pid)] = inst
        return result

    @staticmethod
    def _is_chat_instance(inst: Any) -> bool:
        cfg = getattr(inst, "provider_config", None)
        if not isinstance(cfg, dict):
            return False
        ptype = cfg.get("provider_type")
        return ptype in (None, "", "chat_completion")

    def _ordered_ids(self, mode: str = MODE_TEXT) -> list[str]:
        """按策略生成某一类的候选顺序（未过滤冷却与可用性）。"""
        declared = self._list_of(mode)
        if mode == MODE_TEXT:
            if declared:
                ids = list(declared)
                # 严格候选集下绝不追加未列出的模型；只有显式关闭严格模式且
                # auto_append_enabled 为真时才做「全量兜底」。
                if not self._strict_allowlist() and self._cfg_bool(
                    "auto_append_enabled", False
                ):
                    known = set(ids)
                    ids.extend(
                        sorted(pid for pid in self._chat_instances() if pid not in known)
                    )
            else:
                ids = sorted(self._chat_instances())
        else:
            # 图像列表同样「列表顺序即优先级」（表单里可勾选、可用 ↑↓ 排序），
            # 只是不自动追加未列出的模型：视觉能力无法从配置判断，猜错会让请求直接失败。
            ids = list(declared)

        if self._strategy() == "alphabetical":
            ids = sorted(ids)

        # 文本列表：已切换过的模型置顶，保证后续请求优先命中它
        if mode == MODE_TEXT and self._preferred_id:
            if self._preferred_id in ids:
                ids.remove(self._preferred_id)
                ids.insert(0, self._preferred_id)

        # 被 remove 策略移出的模型，本次运行内不再参与候选
        # （文本列表会自动追加未列出的模型，仅删配置挡不住它）
        if self._self_removed:
            ids = [pid for pid in ids if (pid, mode) not in self._self_removed]
        return ids

    def _candidates(self, exclude: set[str], mode: str = MODE_TEXT) -> list[tuple[str, Any]]:
        instances = self._chat_instances()
        limit = max(1, self._cfg_int("max_chain", 4))
        result: list[tuple[str, Any]] = []
        for pid in self._ordered_ids(mode):
            if pid in exclude or len(result) >= limit:
                continue
            inst = instances.get(pid)
            if inst is None:
                continue
            if self._in_cooldown(pid):
                continue
            result.append((pid, inst))
        return result

    def _in_cooldown(self, pid: str) -> bool:
        return self._cooldown_until.get(pid, 0.0) > time.time()

    def _mark_failed(self, pid: str, reason: str, mode: str = MODE_TEXT) -> None:
        seconds = max(0, self._cfg_int("cooldown_seconds", 180))
        self._cooldown_until[pid] = time.time() + seconds
        self._last_reason[pid] = reason[:200]
        self.logger.warning(
            "[自动切换模型] %s 判定失败（%s），冷却 %s 秒后恢复候选资格。",
            pid,
            reason[:120] or "未知原因",
            seconds,
        )
        self._note_failure(pid, mode)

    def _mark_ok(self, pid: str, mode: str | None = None) -> None:
        if mode is None:
            for key in [item for item in self._failure_counts if item[0] == pid]:
                self._failure_counts.pop(key, None)
        else:
            self._failure_counts.pop((pid, mode), None)
        if pid in self._cooldown_until:
            self._cooldown_until.pop(pid, None)
            self._last_reason.pop(pid, None)

    # ------------------------------------------------------------ 失败处理策略

    def _failure_policy(self) -> str:
        """none = 只冷却（旧行为）；demote = 降到列表末尾；remove = 移出列表。"""
        raw = str(self._cfg("failure_policy", "demote") or "").strip().lower()
        return raw if raw in {"none", "demote", "remove"} else "demote"

    def _failure_threshold(self) -> int:
        return max(1, min(self._cfg_int("failure_threshold", 3), 20))

    def _note_failure(self, pid: str, mode: str) -> None:
        """累计连续失败次数，达到阈值后按配置的策略处理。"""
        policy = self._failure_policy()
        if policy == "none":
            return
        key = (pid, mode)
        count = self._failure_counts.get(key, 0) + 1
        threshold = self._failure_threshold()
        if count < threshold:
            self._failure_counts[key] = count
            self.logger.info(
                "[自动切换模型] %s 连续失败 %s/%s 次，达到阈值后执行 %s。",
                pid,
                count,
                threshold,
                policy,
            )
            return
        self._failure_counts.pop(key, None)
        self._apply_failure_policy(pid, mode, policy, count)

    def _apply_failure_policy(
        self, pid: str, mode: str, policy: str, count: int
    ) -> None:
        """按策略调整该模式下该模型的位置；列表为空时只保留冷却行为。"""
        key = MODE_KEYS[mode]
        path = MODE_PATHS[mode]
        items = self._as_id_list(self._get_path(path))
        if not items:
            self.logger.info(
                "[自动切换模型] %s 连续失败 %s 次，但 %s 为空（跟随 AstrBot 默认），"
                "仅冷却不调整顺序。",
                pid,
                count,
                key,
            )
            return

        if pid not in items:
            # 不在列表里的模型（文本列表可能靠自动追加进来）没有顺序可调
            if policy == "remove":
                self._self_removed.add((pid, mode))
                self.logger.warning(
                    "[自动切换模型] %s 连续失败 %s 次，已在本次运行内移出%s候选。",
                    pid,
                    count,
                    MODE_NAMES[mode],
                )
            return

        if policy == "remove":
            if len(items) <= 1:
                self.logger.warning(
                    "[自动切换模型] %s 连续失败 %s 次，但它是%s列表里唯一的模型，"
                    "跳过移除以免列表变空后退回全量候选。",
                    pid,
                    count,
                    MODE_NAMES[mode],
                )
                return
            items = [item for item in items if item != pid]
            self._self_removed.add((pid, mode))
            action = f"从{MODE_NAMES[mode]}列表中移除"
        else:
            items = [item for item in items if item != pid]
            items.append(pid)
            action = f"降到{MODE_NAMES[mode]}列表末尾"

        self._set_path(path, items)
        saved = self._save_config_quietly("失败策略调整")
        self.logger.warning(
            "[自动切换模型] %s 连续失败 %s 次，已%s → [%s]%s。",
            pid,
            count,
            action,
            "、".join(items),
            "（已落盘）" if saved else "",
        )

    def _save_config_quietly(self, action: str) -> bool:
        """把内存配置写回磁盘；失败只记日志，不影响主流程。"""
        save = getattr(self.config, "save_config", None)
        if not callable(save):
            return False
        try:
            save()
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[自动切换模型] %s后落盘失败：%s", action, exc)
            return False

    # ------------------------------------------------------------------ 切换后处理

    async def _on_switch_success(
        self, failed_pid: str, new_pid: str, mode: str = MODE_TEXT
    ) -> None:
        self._switch_count += 1
        self._last_switch = (time.time(), failed_pid, new_pid, mode)
        self._current_id = new_pid
        self._mark_ok(new_pid, mode)

        if mode != MODE_TEXT:
            # 图像 / 音频请求的切换只在本次生效：不写偏好、不改主模型，
            # 否则一次图片请求的失败会把后续纯文本请求也带到多模态模型上。
            self.logger.info(
                "[自动切换模型] 已切换：%s → %s（%s请求，仅本次生效）",
                failed_pid,
                new_pid,
                MODE_NAMES[mode],
            )
            return

        # 第一步：内存偏好立即生效——后续请求直接走新模型，不等任何保存动作
        self._preferred_id = new_pid
        self.logger.info(
            "[自动切换模型] 已切换：%s → %s（第 %s 次，已即时生效）",
            failed_pid,
            new_pid,
            self._switch_count,
        )

        # 第二步：就地改写内存中的主模型配置，使配置与运行时保持一致
        try:
            cfg = self.context.get_config()
            cfg.setdefault("agent_runner", {}).setdefault("config", {}).setdefault("model", {})
            cfg["agent_runner"]["config"]["model"]["provider_id"] = new_pid
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[自动切换模型] 更新内存主模型配置失败：%s", exc)
            return

        # 第三步：仅在开启持久化时落盘，重启后依然生效
        if not self._cfg_bool("persist_switch", True):
            return
        try:
            cfg.save_config()
            self.logger.info("[自动切换模型] 已落盘主模型配置 → %s", new_pid)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[自动切换模型] 落盘主模型配置失败：%s", exc)

    # ------------------------------------------------------------------ 调用链


    def _enforce_allowlist(self, provider: Any, mode: str, attr: str):
        """严格候选集闸门：本次要调的模型不在列表内，就换成列表内的。

        返回 (provider, pid, orig, blocked)。orig 非 None 表示换了实现，调用方需改用它；
        blocked=True 表示列表内一个可用模型都没有，调用方应直接报错，
        而不是退回列表外的模型。
        """
        pid = self._pid(provider)
        if self._pid_allowed(pid, mode):
            return provider, pid, None, False

        alt = self._best_available(mode, allow_cooldown=True)
        if alt is not None:
            new_pid = self._pid(alt)
            self.logger.warning(
                "[自动切换模型] 拦截候选集外的模型 %s（%s请求），改用列表内 %s",
                pid,
                MODE_NAMES.get(mode, mode),
                new_pid,
            )
            return alt, new_pid, self._original_for(alt, attr), False

        if self._allowed_ids(mode):
            self.logger.error(
                "[自动切换模型] %s 里没有已启用的模型，%s请求被拦截，"
                "不会回退到列表外的模型。",
                MODE_KEYS.get(mode, "text_models"),
                MODE_NAMES.get(mode, mode),
            )
            return provider, pid, None, True
        return provider, pid, None, False

    @staticmethod
    def _blocked_response(mode: str) -> Any:
        return LLMResponse(
            role="err",
            completion_text=(
                f"候选模型全部不可用（{MODE_KEYS.get(mode, 'text_models')} 里没有已启用的模型）。"
                "严格候选集已拦截本次请求，没有改用列表外的模型。"
                "请检查模型是否已启用，或在插件配置里补充候选模型。"
            ),
        )

    async def _chat_with_switch(self, provider: Any, orig: Any, args, kwargs):
        pid = self._pid(provider)
        if not (self._patched_ready or _HOOKS_INSTALLED):
            return await orig(provider, *args, **kwargs)

        # 候选集跟着「实际被调用的模型」走：视觉模型失败时继续在图像列表里换，
        # 文本模型失败时仍走 text_models，不会互相串台
        mode = MODE_IMAGE if pid in set(self._list_of(MODE_IMAGE)) else MODE_TEXT

        # 闸门放在 persist 判断之前：内部辅助调用（标题生成等）同样不许越界，
        # 但只有主调用会在被拦截时直接报错。
        provider, pid, new_orig, blocked = self._enforce_allowlist(provider, mode, "text_chat")
        if new_orig is not None:
            orig = new_orig
        if blocked:
            return self._blocked_response(mode)

        # persist=False 是 AstrBot 内部辅助调用（图片转述、标题生成等），
        # 它们失败不该触发主模型切换。
        if kwargs.get("persist") is False:
            return await orig(provider, *args, **kwargs)

        try:
            resp = await orig(provider, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            switch, fragment = self._should_switch(exc)
            if not switch:
                raise
            result = await self._failover(provider, pid, "text_chat", args, kwargs, fragment, mode)
            if result is not None:
                return result
            raise

        switch, fragment = self._should_switch(resp)
        if not switch:
            self._mark_ok(pid, mode)
            return resp

        result = await self._failover(provider, pid, "text_chat", args, kwargs, fragment, mode)
        return result if result is not None else resp

    async def _stream_with_switch(self, provider: Any, orig: Any, args, kwargs):
        pid = self._pid(provider)
        if not (self._patched_ready or _HOOKS_INSTALLED):
            async for chunk in orig(provider, *args, **kwargs):
                yield chunk
            return

        mode = MODE_IMAGE if pid in set(self._list_of(MODE_IMAGE)) else MODE_TEXT
        provider, pid, new_orig, blocked = self._enforce_allowlist(provider, mode, "text_chat_stream")
        if new_orig is not None:
            orig = new_orig
        if blocked:
            yield self._blocked_response(mode)
            return

        if kwargs.get("persist") is False:
            async for chunk in orig(provider, *args, **kwargs):
                yield chunk
            return

        produced = False
        try:
            async for chunk in orig(provider, *args, **kwargs):
                produced = True
                yield chunk
            return
        except Exception as exc:  # noqa: BLE001
            switch, fragment = self._should_switch(exc)
            if produced or not switch:
                raise

        self._mark_failed(pid, fragment, mode)
        for cand_id, cand in self._candidates({pid}, mode):
            try:
                cand_orig = self._original_for(cand, "text_chat_stream")
                async for chunk in cand_orig(cand, *args, **kwargs):
                    yield chunk
                await self._on_switch_success(pid, cand_id, mode)
                return
            except Exception as exc:  # noqa: BLE001
                self._mark_failed(cand_id, str(exc), mode)
                continue
        self.logger.error("[自动切换模型] 流式请求：%s 失败且无可切换模型。", pid)
        raise

    async def _failover(
        self,
        provider: Any,
        pid: str,
        attr: str,
        args,
        kwargs,
        reason: str,
        mode: str = MODE_TEXT,
    ):
        """按顺序尝试候选模型，全部失败返回 None。"""
        async with self._lock:
            self._mark_failed(pid, reason, mode)
            candidates = self._candidates({pid}, mode)

            if not candidates:
                if self._cfg_bool("warn_when_empty", True):
                    hint = (
                        f"（{MODE_KEYS.get(mode, 'text_models')} 里没有其它可用模型）"
                        if mode != MODE_TEXT
                        else "（全部冷却中或未配置）"
                    )
                    self.logger.error(
                        "[自动切换模型] %s 失败，且当前没有可用候选模型%s。", pid, hint
                    )
                return None

            for cand_id, cand in candidates:
                cand_orig = self._original_for(cand, attr)
                try:
                    resp = await cand_orig(cand, *args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(
                        "[自动切换模型] 候选 %s 调用失败：%s", cand_id, str(exc)[:150]
                    )
                    self._mark_failed(cand_id, str(exc), mode)
                    continue

                if self._is_err_response(resp):
                    self.logger.warning(
                        "[自动切换模型] 候选 %s 返回错误响应：%s",
                        cand_id,
                        self._text_of(resp)[:150],
                    )
                    self._mark_failed(cand_id, self._text_of(resp), mode)
                    continue

                await self._on_switch_success(pid, cand_id, mode)
                return resp

            self.logger.error("[自动切换模型] %s 失败，全部候选均不可用。", pid)
            return None

    # ------------------------------------------------------------------ 指令

    # ------------------------------------------------------------ 图片补挂 / 转码
    def _install_image_observer(self) -> None:
        """旁路观察每条入站消息：只要带图就先记下来。

        为什么不用 `@filter.event_message_type(ALL)`：那类 handler 会参与唤醒判定，
        加了等于把所有群消息都变成「需要响应」。这里改在 EventBus 打日志的位置旁挂，
        只读、不改事件、不影响唤醒，即使后续被别的插件 `stop_event()` 也照样记得住。
        """
        if self._image_hook_orig is not None:
            return
        try:
            from astrbot.core.event_bus import EventBus
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[自动切换模型] 无法挂载图片观察器（%s），图片补挂退化为仅请求阶段。", exc)
            return
        original = getattr(EventBus, "_print_event", None)
        if not callable(original):
            self.logger.warning("[自动切换模型] EventBus._print_event 不存在，图片补挂退化为仅请求阶段。")
            return

        plugin = self

        def patched_print_event(bus_self, event, conf_name):
            try:
                active = _ACTIVE_PLUGIN or plugin
                active._observe_event_images(event)
            except Exception:  # noqa: BLE001  观察失败绝不能影响主流程
                pass
            return original(bus_self, event, conf_name)

        patched_print_event._switch_model_hook = True
        self._image_hook_orig = original
        EventBus._print_event = patched_print_event
        self.logger.info(
            "[自动切换模型] 图片观察器已挂载：入站图片会缓存 %d 秒，供下一条 @ 提问补挂。",
            self._cfg_int("image_bridge_ttl", 120),
        )

    def _remove_image_observer(self) -> None:
        if self._image_hook_orig is None:
            return
        try:
            from astrbot.core.event_bus import EventBus

            EventBus._print_event = self._image_hook_orig
        except Exception:  # noqa: BLE001
            pass
        self._image_hook_orig = None

    @staticmethod
    def _image_ref(comp: Any) -> str:
        """取一个图片引用，优先选“活得久”的那一种。

        顺序：http(s) 链接 → file/file:// → 本地 path。
        本地 path 由 AstrBot 的事件级临时目录托管，事件结束即可能被清理，
        缓存 20 秒后再用就会 FileNotFoundError；CDN 链接则还能重新下载。
        """
        for attr in ("url", "file"):
            value = getattr(comp, attr, None)
            if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
                return value.strip()
        for attr in ("file", "path"):
            value = getattr(comp, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _ref_key(ref: str) -> str:
        """把图片引用归一化成去重键：同一张图的不同写法得到同一个键。

        QQ 的图片链接形如 `https://multimedia.nt.qq.com.cn/download?appid=...&fileid=...&rkey=...`，
        所有图片的路径都一样，**唯一身份在 `fileid`**；`rkey` 每次刷新都会变，不能进键。
        旧实现直接砍掉查询串，导致「不同图片被合并成一张」和「同一张图的 URL/本地路径
        被算成两张」两种错法同时存在。
        """
        text = str(ref or "").strip()
        if not text:
            return ""
        if text.startswith("file://"):
            text = text[7:]
        if text.startswith(("http://", "https://")):
            parsed = urllib.parse.urlparse(text)
            query = urllib.parse.parse_qs(parsed.query)
            ident = (query.get("fileid") or [""])[0].strip()
            if ident:
                return "url:" + parsed.netloc.lower() + "/" + ident
            # 无 fileid 的普通链接：保留查询串，避免把不同资源合并
            return "url:" + parsed.path.lower() + ("?" + parsed.query.lower() if parsed.query else "")
        if text.startswith("base64://"):
            return "b64:" + hashlib.sha1(text[9:].encode("utf-8", "replace")).hexdigest()
        return "path:" + os.path.basename(text).lower()

    def _collect_images(self, event: AstrMessageEvent) -> list[str]:
        """从消息链里取出图片引用，并去掉同一条消息内的重复项。

        QQ 发动图时 NapCat 常在一条消息里同时给出 `image` 段和 `file` 段，
        两者指向同一张图（一个 CDN 链接、一个容器内本地路径）。旧逻辑把它们
        当成两张：链接那张可能因截断被 AstrBot 跳过，本地路径那张在 AstrBot
        容器里根本不存在（FileNotFoundError），最终模型一张图也拿不到。
        这里按「有 image 段就不用 file 段」+ 归一化键去重，一张图只算一次。
        """
        try:
            chain = list(getattr(event.message_obj, "message", None) or [])
        except Exception:  # noqa: BLE001
            return []

        primary: list[str] = []
        fallback: list[str] = []
        for comp in chain:
            name = type(comp).__name__
            if name == "Image":
                ref = self._image_ref(comp)
                if ref:
                    primary.append(ref)
            elif name == "File" and self._file_suffix(comp) in _IMAGE_EXT:
                ref = self._image_ref(comp)
                if ref:
                    fallback.append(ref)

        # image 段优先；整条消息没有 image 段时才退回 file 段
        refs = primary or fallback
        unique: list[str] = []
        seen: set[str] = set()
        for ref in refs:
            key = self._ref_key(ref)
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(ref)
        return unique

    def _observe_event_images(self, event: AstrMessageEvent) -> None:
        """事件总线旁路：入站消息带图就缓存（同步、只读）。"""
        if not self._cfg_bool("enabled", True):
            return
        if self._cfg_int("image_bridge_ttl", 120) <= 0:
            return
        umo = getattr(event, "unified_msg_origin", None)
        if not umo:
            return
        try:  # 机器人自己发的消息（表情包等）不算用户输入
            if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
                return
        except Exception:  # noqa: BLE001
            pass
        refs = self._capture_images_once(event, umo)
        if refs:
            self.logger.debug(
                "[自动切换模型] 观察到 %d 张图片，已缓存待补挂（umo=%s）", len(refs), umo
            )

    def _prune_images(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cache = getattr(self, "_image_cache", None)
        if not cache:
            return
        for key, (_, expire) in list(cache.items()):
            if expire < now:
                cache.pop(key, None)

    def _capture_images_once(self, event: AstrMessageEvent, umo: str) -> list[str]:
        """同一条消息只采集一次，返回本次新采集到的引用。

        观察器与 `on_waiting_llm_request` 都会调用；靠 message_id 判重，
        避免同一张图被「URL 版」和「本地文件名版」各记一次。
        """
        msg_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        if msg_id and self._image_msg_id.get(umo) == msg_id:
            return []
        refs = self._collect_images(event)
        if not refs:
            return []
        if msg_id:
            self._image_msg_id[umo] = msg_id
        self._store_images(umo, refs)
        return refs

    def _touch_images(self, umo: str, ttl: int) -> None:
        """只延长已缓存图片的有效期，不重复采集（等待期间用）。"""
        item = (getattr(self, "_image_cache", None) or {}).get(umo)
        if not item:
            return
        self._image_cache[umo] = (item[0], time.time() + max(0, ttl))

    def _store_images(self, umo: str, refs: list[str], ttl: int | None = None) -> int:
        if ttl is None:
            ttl = self._cfg_int("image_bridge_ttl", 120)
        if ttl <= 0 or not refs:
            return 0
        if not isinstance(getattr(self, "_image_cache", None), dict):
            self._image_cache = {}
        now = time.time()
        self._prune_images(now)
        current, _ = self._image_cache.get(umo, ([], 0.0))
        merged: list[str] = []
        seen_keys: set[str] = set()
        for ref in list(current) + [r for r in refs if r]:
            key = self._ref_key(ref)
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            merged.append(ref)
        # 只保留最近几张，避免连发图片把请求撑爆
        merged = merged[-_MAX_BRIDGE_IMAGES:]
        self._image_cache[umo] = (merged, now + ttl)
        return len(refs)

    def _peek_images(self, umo: str | None) -> list[str]:
        if not umo:
            return []
        now = time.time()
        self._prune_images(now)
        item = (getattr(self, "_image_cache", None) or {}).get(umo)
        if not item or item[1] < now:
            return []
        return list(item[0])

    def _take_images(self, umo: str | None) -> list[str]:
        """取走并清空（一次性补挂，避免后续每条消息都重复带图）。"""
        refs = self._peek_images(umo)
        if umo:
            (getattr(self, "_image_cache", None) or {}).pop(umo, None)
        return refs

    def _has_cached_images(self, umo: str | None) -> bool:
        return bool(self._peek_images(umo))

    @staticmethod
    def _file_suffix(comp: Any) -> str:
        raw = ""
        for attr in ("name", "file_", "file", "url"):
            value = getattr(comp, attr, None)
            if isinstance(value, str) and value:
                raw = value
                break
        if not raw:
            return ""
        match = re.search(r"(\.[A-Za-z0-9]{2,5})(?:$|[?#])", raw)
        return match.group(1).lower() if match else ""

    def _should_defer_image(self, event: AstrMessageEvent) -> bool:
        """纯图片消息是否该等用户追问，而不是立刻回一句看图感想。"""
        if not self._cfg_bool("image_wait_followup", True):
            return False
        # 不缓存就别扣住消息，否则图直接丢了
        if self._cfg_int("image_bridge_ttl", 120) <= 0:
            return False
        if (getattr(event, "message_str", "") or "").strip():
            return False

        try:
            chain = list(getattr(event.message_obj, "message", None) or [])
        except Exception:  # noqa: BLE001
            return False
        for comp in chain:
            name = type(comp).__name__
            if name in ("Reply", "At", "AtAll"):
                # 已经 @ 了机器人 / 引用了某条消息 → 用户就是在发起提问，正常处理
                return False
        return True

    def _wait_seconds(self) -> int:
        """纯图片消息在自动回复前等多久。

        与 `image_bridge_ttl`（补挂有效期）解耦：等待只影响响应速度，
        补挂有效期影响「发图后多久内追问还能把图带上」，两者诉求不同。
        未配置时退回旧行为（沿用 image_bridge_ttl）。
        """
        raw = self._cfg("image_wait_seconds", None)
        if raw is None or str(raw).strip() == "":
            return max(0, min(self._cfg_int("image_bridge_ttl", 120), 600))
        return max(0, min(self._cfg_int("image_wait_seconds", 10), 600))

    def _bridge_cached_images(self, event: AstrMessageEvent, req: Any) -> None:
        """把先前缓存的图片补挂到本次请求上。

        `OnLLMRequestEvent` 早于 `prepare_request_images()`，所以这里改 `req.image_urls`
        能真正进入模型输入；晚于此再改就来不及了。
        """
        if self._cfg_int("image_bridge_ttl", 120) <= 0:
            return
        umo = getattr(event, "unified_msg_origin", None)
        # 本条消息自己就带图（纯图片消息等待超时后自动回复走的就是这条）：
        # req 里已经有这张图，再从缓存补一份只会得到「本地路径 + CDN URL」两份引用；
        # 同时把缓存清掉，避免这张旧图被粘到后面的文字提问上。
        if self._collect_images(event):
            (self._image_cache or {}).pop(umo, None)
            return
        if not self._has_cached_images(umo):
            return

        current = [str(x) for x in (getattr(req, "image_urls", None) or []) if x]
        cached = self._take_images(umo)
        if cached:
            # 图片已被本次请求取走，说明「追问」到了；仍在等待的那条纯图片消息应放弃回复，
            # 否则会出现一条图片两次回答。
            self._defer_tokens[umo] = self._defer_tokens.get(umo, 0) + 1
        merged: list[str] = []
        seen_keys: set[str] = set()
        for ref in current + cached:
            key = self._ref_key(ref)
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            merged.append(ref)
        if len(merged) <= len(current):
            return
        req.image_urls = merged
        added = len(merged) - len(current)
        event.set_extra("_switch_model_bridged_images", added)
        self.logger.info(
            "[自动切换模型] 已把先前记录的 %d 个图片引用补挂到本次请求（本次共 %d 个）",
            added,
            len(merged),
        )

    def _safe_image_dir(self) -> str:
        """插件自己的图片目录：<data>/plugin_data/<插件名>/image_safe。

        __file__ 是 <data>/plugins/<插件名>/main.py，因此要向上退三级才到 <data>。
        """
        data_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        return os.path.join(data_dir, "plugin_data", PLUGIN_NAME, "image_safe")

    def _safe_image_max_side(self) -> int:
        """转码后的最长边，跟随 AstrBot 自己的图片压缩设置。"""
        try:
            opts = (
                (self.context.get_config().get("provider_settings") or {})
                .get("image_compress_options")
                or {}
            )
            value = int(opts.get("max_size", 1024))
            return min(max(value, 256), 2048)
        except Exception:  # noqa: BLE001
            return 1024

    async def _prepare_images_for_model(self, req: Any) -> None:
        """请求发出前把图片统一换成合规 PNG，并收敛重复项。

        AstrBot 的 prepare_model_image 对动图会 load() 全部帧，文件尾部稍有截断
        就抛 OSError 并把整张图跳过，模型只能回「看不到图」。这里先自己解码一次：
        动图取首帧、容忍截断、本地路径失效时回退到链接重下，产出标准 PNG。
        AstrBot 拿到的是合规静态图，会走「原样复用」分支，不再触发解码失败。

        收敛分两级：
        1. 按引用键去重（同一 URL 的 rkey 变体）；
        2. 按转码结果去重（不同引用指向同一份字节），再丢弃同一张图的缩略图——
           QQ 常在同一条消息里同时上报原图与预览图，两张都送进模型是纯浪费。
        """
        if not self._cfg_bool("image_safe_transcode", False):
            return
        refs = [str(x) for x in (getattr(req, "image_urls", None) or []) if x]
        if not refs:
            return

        items: list[tuple[str, bool]] = []  # (最终引用, 是否由我们转码)
        seen_keys: set[str] = set()
        for ref in refs:
            key = self._ref_key(ref)
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            safe = await self._to_model_safe_image(ref)
            items.append((safe or ref, bool(safe and safe != ref)))

        items = self._drop_same_result(items)
        after_same = len(items)
        items = self._drop_thumbnails(items) if self._cfg_bool(
            "image_drop_thumbnails", True
        ) else items

        out = [ref for ref, _ in items]
        converted = sum(1 for _, done in items if done)
        if converted or len(out) != len(refs):
            req.image_urls = out
            self.logger.info(
                "[自动切换模型] 图片预处理：收到 %d 个引用 → 去重后 %d 张 → 丢弃缩略图后 %d 张"
                "（其中转码 %d 张）",
                len(refs),
                after_same,
                len(out),
                converted,
            )

    @staticmethod
    def _drop_same_result(items: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
        """不同引用转出同一个文件（内容相同）时只保留一份。"""
        out: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for ref, done in items:
            if ref in seen:
                continue
            seen.add(ref)
            out.append((ref, done))
        return out

    def _drop_thumbnails(self, items: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
        """丢掉同一条消息里「同一张图的预览图」。

        判据（三条同时满足才丢，尽量不误伤）：
        - 两张都是本地文件（只处理我们自己转码出来的图）；
        - 宽高比相对误差小于 5%；
        - 小图面积不足大图的一半。
        """
        sized: list[tuple[int, float, tuple[str, bool]]] = []
        for item in items:
            path = item[0]
            if not os.path.isfile(path):
                continue
            size = self._image_pixel_size(path)
            if not size:
                continue
            width, height = size
            if width <= 0 or height <= 0:
                continue
            sized.append((width * height, width / height, item))
        if len(sized) < 2:
            return items

        drop: set[str] = set()
        for area, ratio, (ref, _) in sized:
            for other_area, other_ratio, (other_ref, _) in sized:
                if ref == other_ref or ref in drop:
                    continue
                if other_area <= area:
                    continue
                if area * 2 > other_area:
                    continue
                if abs(ratio - other_ratio) / other_ratio > 0.05:
                    continue
                drop.add(ref)
                self.logger.info(
                    "[自动切换模型] 丢弃缩略图：%s（%d 像素，主图 %d 像素）",
                    os.path.basename(ref),
                    area,
                    other_area,
                )
                break
        if not drop:
            return items
        return [item for item in items if item[0] not in drop]

    @staticmethod
    def _image_pixel_size(path: str) -> tuple[int, int] | None:
        """只读文件头拿宽高，不解码像素。"""
        try:
            from PIL import Image as PILImage

            with PILImage.open(path) as image:
                return image.size
        except Exception:  # noqa: BLE001
            return None

    async def _to_model_safe_image(self, ref: str) -> str | None:
        """把一个图片引用转成本地合规 PNG；失败返回 None（调用方保留原引用）。"""
        text = str(ref or "").strip()
        if not text:
            return None

        out_dir = self._safe_image_dir()
        if text.startswith(out_dir):
            return text if os.path.isfile(text) else None

        key = self._ref_key(text)
        hit = self._safe_image_cache.get(key)
        if hit and hit[1] > time.time() and os.path.isfile(hit[0]):
            return hit[0]

        try:
            raw = await self._read_image_ref(text)
            if not raw:
                self.logger.warning(
                    "[自动切换模型] 图片取不到，已跳过：%s", text[-60:]
                )
                return None
            safe = await asyncio.to_thread(self._encode_safe_image, raw)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[自动切换模型] 图片转码异常（%s）：%s", type(exc).__name__, str(exc)[:120]
            )
            return None
        if safe and key:
            self._safe_image_cache[key] = (safe, time.time() + 600)
        return safe

    async def _read_image_ref(self, ref: str) -> bytes | None:
        """读出图片字节：base64 / 本地文件 / 链接下载，依次尝试。"""
        if ref.startswith("base64://"):
            try:
                return base64.b64decode(ref[9:])
            except Exception:  # noqa: BLE001
                return None

        path = ref[7:] if ref.startswith("file://") else ref
        if not path.startswith(("http://", "https://")):
            try:
                if os.path.isfile(path):
                    if os.path.getsize(path) > _MAX_SAFE_IMAGE_BYTES:
                        return None
                    with open(path, "rb") as fh:
                        return fh.read()
            except OSError:
                return None
            # 本地路径失效（事件结束被清理，或属于 NapCat 容器）→ 交给下载兜底
            return None

        return await self._download_image(path)

    async def _download_image(self, url: str) -> bytes | None:
        try:
            import httpx
        except Exception:  # noqa: BLE001
            return None
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        return None
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > _MAX_SAFE_IMAGE_BYTES:
                            return None
                    return bytes(buf)
        except Exception:  # noqa: BLE001
            return None

    def _encode_safe_image(self, raw: bytes) -> str | None:
        """同步解码并转 PNG：动图取首帧、容忍截断。CPU 密集，放在线程里跑。"""
        try:
            from PIL import Image as PILImage
            from PIL import ImageFile, ImageOps
        except Exception:  # noqa: BLE001
            return None
        if not raw or len(raw) > _MAX_SAFE_IMAGE_BYTES:
            return None

        out_dir = self._safe_image_dir()
        digest = hashlib.sha1(raw).hexdigest()
        target = os.path.join(out_dir, "safe_%s.png" % digest[:16])
        if os.path.isfile(target):
            return target

        max_side = self._safe_image_max_side()
        tmp = target + ".tmp"
        try:
            with _PIL_TRUNCATE_LOCK:
                previous = ImageFile.LOAD_TRUNCATED_IMAGES
                ImageFile.LOAD_TRUNCATED_IMAGES = True
                try:
                    with PILImage.open(io.BytesIO(raw)) as image:
                        try:
                            image.seek(0)  # 动图只取首帧，避开全帧解码
                        except Exception:  # noqa: BLE001
                            pass
                        oriented = ImageOps.exif_transpose(image)
                        bands = oriented.getbands()
                        if oriented.mode not in ("RGB", "RGBA"):
                            oriented = oriented.convert(
                                "RGBA" if "A" in bands else "RGB"
                            )
                        oriented.thumbnail((max_side, max_side))
                        os.makedirs(out_dir, exist_ok=True)
                        oriented.save(tmp, format="PNG", optimize=True)
                finally:
                    ImageFile.LOAD_TRUNCATED_IMAGES = previous
            os.replace(tmp, target)
        except Exception as exc:  # noqa: BLE001
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            self.logger.debug(
                "[自动切换模型] 转码放弃（%s）：%s", type(exc).__name__, str(exc)[:100]
            )
            return None

        self._prune_safe_images(out_dir)
        return target

    def _prune_safe_images(self, out_dir: str, keep: int = 300) -> None:
        """兜底清理：目录里文件过多时删最旧的，避免长期运行堆满存储。"""
        try:
            files = [
                os.path.join(out_dir, name)
                for name in os.listdir(out_dir)
                if name.startswith("safe_") and name.endswith(".png")
            ]
            if len(files) <= keep:
                return
            files.sort(key=lambda p: os.path.getmtime(p))
            for path in files[: len(files) - keep]:
                os.remove(path)
        except OSError:
            return

    @filter.on_waiting_llm_request()
    async def on_waiting_llm_request(self, event: AstrMessageEvent, *args, **kwargs) -> None:
        """纯图片消息先不抢答：缓存图片，等用户追问时一起送给模型。"""
        if not self._cfg_bool("enabled", True):
            return
        umo = getattr(event, "unified_msg_origin", None)
        if not umo:
            return
        # 先登记「本次带图」：本条带图，或本会话还有待补挂的图
        if self._collect_images(event) or self._has_cached_images(umo):
            self._mark_image_request(umo)
        if event.get_extra("_switch_model_defer_passthrough"):
            return
        if not self._should_defer_image(event):
            return
        # 观察器通常已经存过；这里用 message_id 兜底，同一条消息不会重复写
        self._capture_images_once(event, umo)
        await self._defer_or_reply(event, umo)

    async def _defer_or_reply(self, event: AstrMessageEvent, umo: str) -> bool:
        """纯图片消息：等追问，或超时后自己回复。

        返回 True 表示「超时了，继续走管线，本条消息自己回答」；
        返回 False 表示本次不该继续（已拦截等追问，或追问已经回答过了）。
        """
        wait = self._wait_seconds()
        bridge_ttl = max(0, self._cfg_int("image_bridge_ttl", 120))

        # 关闭自动回复 → 保持旧行为：扣住消息等追问
        if not self._cfg_bool("image_defer_auto_reply", True):
            event.stop_event()
            self.logger.info(
                "[自动切换模型] 收到纯图片消息，已记录 %d 个图片引用，等你追问时一起送给模型（%d 秒内有效）",
                len(self._peek_images(umo)),
                bridge_ttl,
            )
            return False

        # 等待时长填 0 = 不等待，立刻看图回答
        if wait <= 0:
            self.logger.info("[自动切换模型] 等待时长设为 0，跳过等待，直接解析图片并回复。")
            return True

        wait = max(1, wait)
        # 缓存要活到等待结束之后，否则解析阶段拿不到图
        keep = max(wait, bridge_ttl) + 120
        self._touch_images(umo, keep)
        token = self._defer_tokens.get(umo, 0)
        self.logger.info(
            "[自动切换模型] 收到纯图片消息，已记录 %d 个图片引用；先等 %d 秒，"
            "期间追问则合并回答，超时则自动解析图片并回复。",
            len(self._peek_images(umo)),
            wait,
        )
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            raise

        if self._defer_tokens.get(umo, 0) != token:
            # 等待期间图片已被另一条请求取走 → 那条请求已经回答，这里不再重复
            event.stop_event()
            self.logger.info(
                "[自动切换模型] 等待期间已收到追问，本次图片消息交由追问一起处理，不再重复回复。"
            )
            return False

        event.set_extra("_switch_model_defer_passthrough", True)
        self.logger.info("[自动切换模型] 等待 %d 秒未收到追问，开始解析图片并直接回复。", wait)
        return True


    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("switchmodel")
    async def switchmodel_cmd(self, event: AstrMessageEvent, arg: str = ""):
        """模型切换插件管理指令。

        用法：/switchmodel [status|list|reset|use|on|off|test]
        """
        parts = (arg or "").split()
        sub = parts[0].lower() if parts else "status"

        if sub in ("status", "状态"):
            yield event.plain_result(self._render_status())
        elif sub in ("list", "列表"):
            target = parts[1].lower() if len(parts) >= 2 else MODE_TEXT
            target = target if target in MODE_KEYS else MODE_TEXT
            yield event.plain_result(self._render_list(target))
        elif sub in ("reset", "重置"):
            self._cooldown_until.clear()
            self._last_reason.clear()
            self._current_id = None
            self._preferred_id = None
            yield event.plain_result("已清空全部冷却状态与运行时偏好，主模型回到配置值。")
        elif sub in ("on", "off"):
            self._set_path(CFG_PATHS["enabled"], sub == "on")
            yield event.plain_result(
                f"插件已临时{'启用' if sub == 'on' else '停用'}"
                f"（运行时开关，重启后以配置文件为准）。"
            )
        elif sub in ("use", "切换") and len(parts) >= 2:
            target = parts[1]
            if target not in self._chat_instances():
                yield event.plain_result(f"未找到模型：{target}")
                return
            self._preferred_id = target
            self._current_id = target
            self._mark_ok(target)
            cfg = self.context.get_config()
            cfg.setdefault("agent_runner", {}).setdefault("config", {}).setdefault("model", {})
            cfg["agent_runner"]["config"]["model"]["provider_id"] = target
            if self._cfg_bool("persist_switch", True):
                cfg.save_config()
            yield event.plain_result(f"已切换到 {target}（立即生效）")
        elif sub in ("test", "测试"):
            yield event.plain_result(await self._selftest(parts[1] if len(parts) >= 2 else None))
        else:
            yield event.plain_result(
                "用法：/switchmodel status|list|reset|use <模型ID>|on|off|test [模型ID]"
            )

    def _render_status(self) -> str:
        policy = self._failure_policy()
        policy_text = (
            "只冷却，不改列表"
            if policy == "none"
            else f"连续 {self._failure_threshold()} 次后调整列表"
        )
        sync_on = self._cfg_bool("auto_sync_provider_models", True)
        sync_interval = max(5, min(self._cfg_int("auto_sync_interval_seconds", 20), 600))
        lines = [
            f"版本：{VERSION}",
            f"启用：{'是' if self._cfg_bool('enabled', True) else '否'}",
            f"排序策略：{self._strategy()}（{STRATEGY_HELP[self._strategy()]}）",
            f"关键词：{len(self._keywords())} 条 / 模式 {self._cfg('match_mode', 'substring')}",
            f"冷却：{self._cfg_int('cooldown_seconds', 180)} 秒 / 单次最多试 {self._cfg_int('max_chain', 4)} 个",
            f"失败策略：{policy}（{policy_text}）",
            f"严格候选集：{'开（只调用列表内模型）' if self._strict_allowlist() else '关（允许回退到列表外）'}",
            f"自动同步：{'开' if sync_on else '关'}（每 {sync_interval} 秒检查 AstrBot 模型设置）",
            f"持久化切换：{'开' if self._cfg_bool('persist_switch', True) else '关（仅本次运行有效）'}",
            "",
            f"文本列表：{len(self._list_of(MODE_TEXT))} 个"
            f"{'（未配置 → 全部已启用对话模型）' if not self._list_of(MODE_TEXT) else ''}",
            "",
            f"运行时偏好：{self._preferred_id or '（未切换，跟随配置）'}",
            f"配置主模型：{self._config_provider_id() or '（未设置）'}",
            f"钩子状态：Provider {len(self._patched)} 个方法 / 解析拦截 "
            f"{'已启用' if self._resolve_async_orig else '未启用'}",
            f"累计切换：{self._switch_count} 次",
        ]
        if self._last_switch:
            ts, old, new, _mode = self._last_switch
            lines.append(
                f"最近切换：{time.strftime('%H:%M:%S', time.localtime(ts))}  {old} → {new}"
            )
        active = [pid for pid, t in self._cooldown_until.items() if t > time.time()]
        if active:
            lines.append("冷却中：")
            for pid in active:
                left = int(self._cooldown_until[pid] - time.time())
                lines.append(f"  · {pid}（剩 {left}s）{self._last_reason.get(pid, '')[:40]}")
        return "\n".join(lines)

    def _render_list(self, mode: str = MODE_TEXT) -> str:
        instances = self._chat_instances()
        lines = [f"{MODE_NAMES.get(mode, '文本')}请求的候选顺序（* = 冷却中，× = 未安装）："]
        for idx, pid in enumerate(self._ordered_ids(mode), 1):
            mark = " " if pid in instances else "×"
            if self._in_cooldown(pid):
                mark = "*"
            lines.append(f"{idx:>2}. [{mark}] {pid}")
        if len(lines) == 1:
            lines.append(
                "（未配置，且没有可用的对话模型）" if mode == MODE_TEXT else "（该列表为空）"
            )
        return "\n".join(lines)


    async def _selftest(self, pid: str | None) -> str:
        instances = self._chat_instances()
        if not instances:
            return "没有可用的对话模型，无法自检。"
        target = pid or next(iter(instances))
        if target not in instances:
            return f"未找到模型：{target}"
        provider = instances[target]
        attr = "text_chat"
        original = self._original_for(provider, attr)
        try:
            await original(provider, prompt="ping")
            return f"{target} 调用正常，未触发切换。"
        except Exception as exc:  # noqa: BLE001
            switch, fragment = self._should_switch(exc)
            if not switch:
                return f"{target} 报错但未命中关键词，不会切换：{str(exc)[:120]}"
            result = await self._failover(
                provider, target, attr, tuple(), {"prompt": "ping"}, fragment, MODE_TEXT
            )
            if result is None:
                return f"{target} 命中关键词「{fragment}」，但没有候选模型可切换。"
            return (
                f"{target} 命中关键词「{fragment}」，已切换到 {self._current_id}，"
                f"响应长度 {len(self._text_of(result))} 字。"
            )
