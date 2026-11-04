"""astrbot_plugin_switch_model 1.4.0 —— 模型报错自动切换 + 三类模型各自的手动优先级。

与 2.x 的区别
-------------
2.x 会自动读取每个模型的「模型能力（modalities）」，据此推断请求该路由到谁，
并带有降级 / 报错等一整套逻辑。这一版把这些全部拿掉，回到 1.0.0 的
简单模型：**顺序由你手动配置，插件只负责按顺序执行**。

三份列表
--------
- `text_models`  ：纯文本请求用
- `image_models` ：带图片的请求用
- `audio_models` ：带音频的请求用

请求进来时先判断属于哪一类，再取对应的列表；列表第一个可用模型就是本次要用的模型。
调用报错（命中关键词）时，在同一份列表里往后换。三类互不干扰。

工作原理
--------
1. `on_waiting_llm_request` 看消息链里有没有图片 / 音频，决定本次用哪份列表；
2. 包装 `ProviderManager.get_using_provider(_async)`，把解析结果换成该列表的首选，
   使「配置即生效」不需要保存或重启；
3. 挂钩 `text_chat` / `text_chat_stream`，命中错误关键词时按列表顺序切换；
4. 切换成功后改写 `agent_runner.config.model.model.provider_id`（可选落盘）；
5. 失败模型进入冷却期，冷却结束自动恢复候选资格。

图片补挂（1.4.0）
-----------------
QQ 不允许 `@` 和图片放在同一条消息里，用户只能「先发图 → 再 @ 提问」。而 AstrBot 的
`req.image_urls` 只取当前这一条事件，图片又不会写进对话历史，于是那条 @ 提问的请求里
一张图都没有，模型只能回「图片是空的」。

1.4.0 的做法：在事件总线层旁路记下最近收到的图片，等到下一条同会话的 LLM 请求时，
在 `on_llm_request`（早于 `prepare_request_images`）把图补进 `req.image_urls`；
同时「纯图片消息」默认不抢答，缓存下来等你追问，避免机器人对每张图自顾自评论。

插件只在内存中挂钩，卸载 / 重载后自动还原，不修改 AstrBot 源码。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.manager import ProviderManager
from astrbot.core.provider.provider import Provider

try:  # AstrBot 通过 quart 兼容层把请求上下文注入到插件 Web API 里
    from quart import request as _web_request
except Exception:  # noqa: BLE001  # pragma: no cover
    _web_request = None

PLUGIN_NAME = "astrbot_plugin_switch_model"
VERSION = "1.5.0"

# 插件页 / 接口的挂载前缀：页面里 apiGet("models") 会打到 /api/plug/<PLUGIN_NAME>/models
WEB_API_BASE = f"/{PLUGIN_NAME}"

# 三类请求
MODE_TEXT = "text"
MODE_IMAGE = "image"
MODE_AUDIO = "audio"
MODE_NAMES = {MODE_TEXT: "文本", MODE_IMAGE: "图像", MODE_AUDIO: "音频"}
MODE_KEYS = {
    MODE_TEXT: "text_models",
    MODE_IMAGE: "image_models",
    MODE_AUDIO: "audio_models",
}

# 默认关键词：覆盖额度耗尽、限流、鉴权、余额不足、模型不存在等常见错误
DEFAULT_KEYWORDS = [
    "Free quota exhausted",
    "AllocationQuota.FreeTierOnly",
    "insufficient_quota",
    "insufficient balance",
    "quota",
    "rate limit",
    "RateLimitError",
    "429",
    "403",
    "401",
    "402",
    "invalid_api_key",
    "InvalidApiKey",
    "model_not_found",
    "All chat models failed",
    "service unavailable",
    "502",
    "503",
]

STRATEGY_HELP = {
    "order": "按列表顺序（默认，推荐）",
    "alphabetical": "按模型 ID 字典序（忽略列表顺序）",
}

# 当前生效的插件实例：挂钩是类级别的，热重载后要能指向新实例
_ACTIVE_PLUGIN = None
# 类级别挂钩是否已就位（跨实例共享，热重载后新实例可直接复用已有挂钩）
_HOOKS_INSTALLED = False

# 图片 / 音频扩展名：用于识别以文件形式发来的内容
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif"}
_AUDIO_EXT = {".mp3", ".wav", ".amr", ".silk", ".ogg", ".m4a", ".flac", ".aac", ".opus"}

# 一次最多补挂几张图（与 AstrBot 的 max_quoted_fallback_images 保持一致）
_MAX_BRIDGE_IMAGES = 3

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
    "模型调用报错时按优先级自动切换；文本 / 图像 / 音频三类模型可分别配置优先级",
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

        # umo -> (模式, 过期时间戳)
        self._pending: dict[str, tuple[str, float]] = {}
        # 最近一次消息的模式（解析时拿不到 umo 的兜底）
        self._pending_any: tuple[str, float] | None = None
        self._last_mode: str = MODE_TEXT
        # 「text_models 首行覆盖了 WebUI 主模型」只提示一次，避免刷屏
        self._text_override_warned = False
        # 图片补挂：umo -> (图片引用列表, 过期时间戳)
        self._image_cache: dict[str, tuple[list[str], float]] = {}
        # EventBus._print_event 的原始实现（旁路观察消息里的图片）
        self._image_hook_orig: Any = None

        self._register_web_apis()

    # ------------------------------------------------------------ 可视化配置接口

    def _register_web_apis(self) -> None:
        """注册插件页用的接口，供 pages/model-priority 调用。"""
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            self.logger.warning(
                "[自动切换模型] 当前 AstrBot 版本不支持插件 Web API，"
                "模型优先级页面将不可用（配置仍可在插件配置页手填）。"
            )
            return
        try:
            register(
                f"{WEB_API_BASE}/models",
                self.api_get_models,
                ["GET"],
                "获取三类模型优先级与可选模型集合",
            )
            register(
                f"{WEB_API_BASE}/models",
                self.api_save_models,
                ["POST"],
                "保存三类模型优先级（保持 list[str] 格式）",
            )
            self.logger.info(
                "[自动切换模型] 配置页接口已就绪：GET/POST %s/models（页面：pages/model-priority）",
                WEB_API_BASE,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[自动切换模型] 注册 Web API 失败：%s", exc)

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        global _ACTIVE_PLUGIN
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
        """取一个能交给 prepare_model_image 的图片引用。"""
        for attr in ("path", "file", "url"):
            value = getattr(comp, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _collect_images(self, event: AstrMessageEvent) -> list[str]:
        """从消息链里取出全部图片引用（Image 组件 + 图片类 File）。"""
        try:
            chain = list(getattr(event.message_obj, "message", None) or [])
        except Exception:  # noqa: BLE001
            return []
        refs: list[str] = []
        for comp in chain:
            name = type(comp).__name__
            if name == "Image":
                ref = self._image_ref(comp)
                if ref:
                    refs.append(ref)
            elif name == "File" and self._file_suffix(comp) in _IMAGE_EXT:
                ref = self._image_ref(comp)
                if ref:
                    refs.append(ref)
        return refs

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
        refs = self._collect_images(event)
        if refs:
            self._store_images(umo, refs)
            self.logger.debug(
                "[自动切换模型] 观察到 %d 张图片，已缓存待补挂（umo=%s）", len(refs), umo
            )

    # ------------------------------------------------------------ 图片缓存

    def _prune_images(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cache = getattr(self, "_image_cache", None)
        if not cache:
            return
        for key, (_, expire) in list(cache.items()):
            if expire < now:
                cache.pop(key, None)

    def _store_images(self, umo: str, refs: list[str]) -> int:
        ttl = self._cfg_int("image_bridge_ttl", 120)
        if ttl <= 0 or not refs:
            return 0
        if not isinstance(getattr(self, "_image_cache", None), dict):
            self._image_cache = {}
        now = time.time()
        self._prune_images(now)
        current, _ = self._image_cache.get(umo, ([], 0.0))
        merged = list(dict.fromkeys(list(current) + [r for r in refs if r]))
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
        self.logger.info("[自动切换模型] 已接管 Provider 解析：切换与按类型选模均即时生效。")

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
        """读出 AstrBot 侧三类「用户当前选的模型」。

        文本 = 主对话模型；图像 = 图片转述模型；音频 = 语音识别（STT）模型。
        读取失败一律返回空表，交给调用方静默跳过。
        """
        try:
            cfg = self.context.get_config()
        except Exception:  # noqa: BLE001
            return {}
        if not isinstance(cfg, dict):
            return {}
        return {
            MODE_TEXT: self._dig(
                cfg, ("agent_runner", "config", "model", "provider_id")
            ),
            MODE_IMAGE: self._dig(
                cfg, ("provider_settings", "default_image_caption_provider_id")
            ),
            MODE_AUDIO: self._dig(cfg, ("provider_stt_settings", "provider_id")),
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

        for mode, pid in wanted.items():
            if not pid:
                continue
            if not force and self._synced_providers.get(mode) == pid:
                continue
            self._synced_providers[mode] = pid

            key = MODE_KEYS[mode]
            items = self._as_id_list(self.config.get(key))
            if not items or items[0] == pid:
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
            self.config[key] = items
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

    def _best_available(self, mode: str = MODE_TEXT):
        candidates = self._candidates(set(), mode)
        if not candidates:
            return None
        if mode == MODE_TEXT and self._preferred_id:
            for pid, inst in candidates:
                if pid == self._preferred_id:
                    return inst
        return candidates[0][1]

    def _apply_preferred(self, provider_type, resolved, umo: str | None = None):
        """解析拦截：图像 / 音频请求先按对应列表选模，其余走 1.0 的偏好逻辑。"""
        value = getattr(provider_type, "value", str(provider_type))
        if value != ProviderType.CHAT_COMPLETION.value:
            return resolved

        mode = self._pending_mode(umo)
        if (
            mode == MODE_TEXT
            and self._list_of(MODE_IMAGE)
            and self._has_cached_images(umo)
        ):
            # 本会话有待补挂的图（QQ 的图与 @ 提问分两条发送）→ 本次必须按图像请求选模，
            # 否则会挑到不支持 image 的模型，AstrBot 随后把图裁掉，模型只会回「图片是空的」。
            mode = MODE_IMAGE
            self.logger.debug(
                "[自动切换模型] 检测到待补挂图片，本次按图像请求选模（umo=%s）", umo
            )

        if mode != MODE_TEXT:
            if not self._list_of(mode):
                # 该类型没配列表 → 不干预，保持原有解析结果
                return resolved
            target = self._best_available(mode)
            if target is not None and target is not resolved:
                self.logger.info(
                    "[自动切换模型] 本次是%s请求，按 %s 列表改用 %s",
                    MODE_NAMES[mode],
                    MODE_KEYS[mode],
                    self._pid(target),
                )
                return target
            return resolved

        text_list = self._list_of(MODE_TEXT)
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
                return resolved

        best = self._best_available(MODE_TEXT)
        if best is None or best is resolved:
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
        self.logger.info("[自动切换模型] 已卸载，Provider 钩子已还原。")

    # ------------------------------------------------------------------ 配置读取

    def _cfg(self, key: str, default: Any = None) -> Any:
        value = self.config.get(key, default)
        return default if value is None else value

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是"}
        return bool(value)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _keywords(self) -> list[str]:
        raw = self.config.get("error_keywords")
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

    def _list_of(self, mode: str) -> list[str]:
        """取某一类模型的手动优先级列表。"""
        key = MODE_KEYS.get(mode, "text_models")
        items = self._as_id_list(self.config.get(key))
        if not items and mode == MODE_TEXT:
            # 兼容 1.0.0 的配置：老的 models 列表就是文本列表
            items = self._as_id_list(self.config.get("models"))
        return items

    # ------------------------------------------------ 可选模型集合（供页面下拉使用）

    @staticmethod
    def _modalities_of(inst: Any) -> list[str]:
        """取模型声明的输入模态；空列表表示「未声明」，按不限制处理。"""
        cfg = getattr(inst, "provider_config", None)
        if not isinstance(cfg, dict):
            return []
        raw = cfg.get("modalities")
        if not isinstance(raw, list):
            return []
        return [str(item).strip().lower() for item in raw if str(item).strip()]

    def _model_rows(self) -> list[dict]:
        """列出全部已启用对话模型，并标注它能不能承接三类请求。"""
        now = time.time()
        rows: list[dict] = []
        for pid, inst in self._chat_instances().items():
            cfg = getattr(inst, "provider_config", None)
            cfg = cfg if isinstance(cfg, dict) else {}
            mods = self._modalities_of(inst)
            # modalities 为空 = 不限制（AstrBot 自身也这么判），三类都视为可用
            unrestricted = not mods
            cool_left = max(0, int(self._cooldown_until.get(pid, 0.0) - now))
            rows.append(
                {
                    "id": pid,
                    "model": str(cfg.get("model") or ""),
                    "type": str(cfg.get("type") or ""),
                    "modalities": mods,
                    "declared": bool(mods),
                    "supports": {
                        MODE_TEXT: unrestricted or "text" in mods,
                        MODE_IMAGE: unrestricted or "image" in mods,
                        MODE_AUDIO: unrestricted or "audio" in mods,
                    },
                    "cooling": cool_left > 0,
                    "cooldown_left": cool_left,
                }
            )
        rows.sort(key=lambda row: row["id"].lower())
        return rows

    def _rejected_ids(self) -> dict[str, str]:
        """非对话模型（TTS / STT / embedding / rerank）→ 拒绝原因。"""
        manager = self._manager()
        if manager is None:
            return {}
        rejected: dict[str, str] = {}
        candidates = list(getattr(manager, "provider_insts", []) or [])
        candidates.extend((getattr(manager, "inst_map", {}) or {}).values())
        for inst in candidates:
            if self._is_chat_instance(inst):
                continue
            cfg = getattr(inst, "provider_config", None)
            if not isinstance(cfg, dict):
                continue
            pid = str(cfg.get("id") or "")
            if pid:
                rejected[pid] = str(cfg.get("provider_type") or "非对话模型")
        return rejected

    @staticmethod
    def _empty_text_warning(rows: dict[str, list[str]]) -> str:
        if not any(rows.values()):
            return "三份列表都为空：插件不会改变任何模型选择，全部交回 AstrBot 原逻辑。"
        return ""

    def _validate_lists(self, payload: dict) -> tuple[dict, list[str], list[str]]:
        """校验三份列表，返回 (干净结果, 错误, 提示)。错误非空则不应保存。"""
        errors: list[str] = []
        warnings: list[str] = []
        clean: dict[str, list[str]] = {}
        rows = {row["id"]: row for row in self._model_rows()}
        rejected = self._rejected_ids()

        for mode, key in MODE_KEYS.items():
            raw = payload.get(key, self._list_of(mode))
            if raw is None:
                raw = []
            if not isinstance(raw, list):
                errors.append(f"{key} 必须是字符串数组，收到 {type(raw).__name__}")
                clean[key] = list(self._list_of(mode))
                continue

            kept: list[str] = []
            seen: set[str] = set()
            for item in raw:
                if not isinstance(item, str):
                    errors.append(f"{key} 里混进了非字符串项：{item!r}")
                    continue
                pid = item.strip()
                if not pid:
                    continue
                if pid in seen:
                    warnings.append(f"{MODE_NAMES[mode]}列表里「{pid}」重复，已自动去重。")
                    continue
                if pid in rejected:
                    errors.append(
                        f"「{pid}」是 {rejected[pid]} 类型的模型，不是对话模型，"
                        f"不能放进{MODE_NAMES[mode]}列表。"
                    )
                    continue
                if pid not in rows:
                    errors.append(
                        f"「{pid}」不存在或未启用，请先到「模型提供商」里启用它。"
                    )
                    continue
                seen.add(pid)
                kept.append(pid)

            # 能力提醒：不拦，但要说清楚，免得运行时才发现模型不认图
            if mode in (MODE_IMAGE, MODE_AUDIO):
                for pid in kept:
                    if not rows[pid]["supports"][mode]:
                        mods = rows[pid]["modalities"]
                        warnings.append(
                            f"「{pid}」没有声明支持{MODE_NAMES[mode]}"
                            f"（当前模态：{'/'.join(mods) if mods else '未声明'}），"
                            f"带{MODE_NAMES[mode]}的请求打到它可能失败，建议确认后再保留。"
                        )
            clean[key] = kept

        empty_warning = self._empty_text_warning(clean)
        if empty_warning:
            warnings.append(empty_warning)
        return clean, errors, warnings

    def _models_payload(self) -> dict:
        return {
            "config": {key: self._list_of(mode) for mode, key in MODE_KEYS.items()},
            "options": self._model_rows(),
            "meta": {
                "version": VERSION,
                "strategy": self._strategy(),
                "auto_append_enabled": self._cfg_bool("auto_append_enabled", True),
                "max_chain": self._cfg_int("max_chain", 4),
                "enabled": self._cfg_bool("enabled", True),
                "main_model": self._config_provider_id(),
                "preferred": self._preferred_id,
                "modes": [
                    {
                        "mode": mode,
                        "key": MODE_KEYS[mode],
                        "label": MODE_NAMES[mode],
                    }
                    for mode in (MODE_TEXT, MODE_IMAGE, MODE_AUDIO)
                ],
            },
        }

    async def api_get_models(self) -> dict:
        """GET 返回三份列表 + 全部可选对话模型。"""
        try:
            return _ok(self._models_payload())
        except Exception as exc:  # noqa: BLE001
            self.logger.error("[自动切换模型] 读取模型优先级失败：%s", exc, exc_info=True)
            return _error(f"读取失败：{exc}")

    async def api_save_models(self) -> dict:
        """POST 保存三份列表；格式仍为 list[str]，保存后立即生效。"""
        if _web_request is None:
            return _error("当前环境无法读取请求体，请到插件配置页手填。")
        try:
            payload = await _web_request.get_json(silent=True)
        except Exception as exc:  # noqa: BLE001
            return _error(f"请求体解析失败：{exc}")
        if not isinstance(payload, dict):
            return _error("请求体必须是 JSON 对象。")

        clean, errors, warnings = self._validate_lists(payload)
        if errors:
            return _error("；".join(errors[:6]), {"errors": errors, "warnings": warnings})

        changed = {
            key: value for key, value in clean.items() if self._as_id_list(self.config.get(key)) != value
        }
        try:
            for key, value in clean.items():
                self.config[key] = value
            # 旧键（1.0.0 的 models）已迁移，避免它继续影响文本列表
            if "models" in self.config and "text_models" in clean:
                self.config.pop("models", None)

            # 新顺序要立刻生效：清掉上一次故障切换留下的运行时偏好
            self._preferred_id = None
            self._text_override_warned = False

            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
            else:
                self.logger.warning(
                    "[自动切换模型] 配置对象不支持落盘，改动仅在内存生效。"
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("[自动切换模型] 保存模型优先级失败：%s", exc, exc_info=True)
            return _error(f"保存失败：{exc}")

        self.logger.info(
            "[自动切换模型] 页面保存优先级：文本 %s / 图像 %s / 音频 %s（改动 %s 项）",
            len(clean[MODE_KEYS[MODE_TEXT]]),
            len(clean[MODE_KEYS[MODE_IMAGE]]),
            len(clean[MODE_KEYS[MODE_AUDIO]]),
            len(changed) or "0",
        )
        result = self._models_payload()
        result["warnings"] = warnings
        return _ok(result, "已保存，运行时立即生效")

    # ---------------------------------------------------------------- 请求分类

    def _remember_mode(self, umo: str | None, mode: str) -> None:
        ttl = max(5, self._cfg_int("mode_ttl", 30))
        now = time.time()
        expire = now + ttl
        if umo:
            prev = self._pending.get(umo)
            if (
                prev
                and prev[1] >= now
                and prev[0] in (MODE_IMAGE, MODE_AUDIO)
                and mode == MODE_TEXT
            ):
                # QQ 不允许 @ 和图片同条发送，所以「图」和「提问」必然是两条消息。
                # 后到的纯文本不能把还没过期的图像/音频判定盖掉，否则图就在上下文里
                # 却按文本列表选了不支持图像的模型，AstrBot 会把图裁掉 → 模型回「图片是空的」。
                self._pending[umo] = (prev[0], expire)
            else:
                self._pending[umo] = (mode, expire)
        self._pending_any = (mode, expire)
        self._last_mode = mode
        for key, (_, exp) in list(self._pending.items()):
            if exp < now:
                self._pending.pop(key, None)

    def _pending_mode(self, umo: str | None = None) -> str:
        now = time.time()
        if umo:
            item = self._pending.get(umo)
            if item and item[1] >= now:
                return item[0]
            # 明确知道是哪个会话、但没有它的记录 → 按文本处理。
            # 不能回落到 _pending_any，否则别的会话刚发的一张图会把这里的文本请求也带到视觉模型上。
            return MODE_TEXT
        # 连 umo 都拿不到（图片转述、标题生成等内部调用）才用最近一次判定兜底
        item = self._pending_any
        if item and item[1] >= now:
            return item[0]
        return MODE_TEXT

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

    def _mode_from_event(self, event: AstrMessageEvent) -> str:
        """看消息链里有什么：音频优先，其次图片，都没有就是文本。"""
        has_image = has_audio = False
        try:
            chain = list(getattr(event.message_obj, "message", None) or [])
        except Exception:  # noqa: BLE001
            return MODE_TEXT
        for comp in chain:
            name = type(comp).__name__
            if name == "Record":
                has_audio = True
            elif name == "Image":
                has_image = True
            elif name == "File":
                suffix = self._file_suffix(comp)
                if suffix in _AUDIO_EXT:
                    has_audio = True
                elif suffix in _IMAGE_EXT:
                    has_image = True
        if has_audio:
            return MODE_AUDIO
        if has_image:
            return MODE_IMAGE
        return MODE_TEXT

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

    @filter.on_waiting_llm_request()
    async def on_waiting_llm_request(self, event: AstrMessageEvent, *args, **kwargs) -> None:
        """确定要调用 LLM 时，先判定本次属于哪一类请求。"""
        if not self._cfg_bool("enabled", True):
            return
        mode = self._mode_from_event(event)
        umo = getattr(event, "unified_msg_origin", None)
        self._remember_mode(umo, mode)

        if mode == MODE_IMAGE and umo:
            # 事件总线观察器可能已经存过，这里再存一次用于「观察器挂不上」的兜底（自动去重）
            self._store_images(umo, self._collect_images(event))

        if mode == MODE_TEXT:
            return

        if mode == MODE_IMAGE and self._should_defer_image(event):
            event.stop_event()
            self.logger.info(
                "[自动切换模型] 收到纯图片消息，已缓存 %d 张，等你追问时一起送给模型（%d 秒内有效）",
                len(self._peek_images(umo)),
                self._cfg_int("image_bridge_ttl", 120),
            )
            return

        model_list = self._list_of(mode)
        if not model_list:
            self.logger.warning(
                "[自动切换模型] 本次是%s请求，但 %s 为空，将按原样交给当前模型处理"
                "（图片/音频可能被 AstrBot 按模型能力丢弃）。",
                MODE_NAMES[mode],
                MODE_KEYS[mode],
            )
            return
        self.logger.info(
            "[自动切换模型] 本次是%s请求，使用 %s（首个：%s）",
            MODE_NAMES[mode],
            MODE_KEYS[mode],
            model_list[0],
        )

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any = None, *args, **kwargs) -> None:
        """请求发出前的最后一道加工：抑制正文 @、补挂先到的图片。"""
        if req is None or not self._cfg_bool("enabled", True):
            return
        self._inject_no_text_mention(req)
        self._bridge_cached_images(event, req)

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

    def _bridge_cached_images(self, event: AstrMessageEvent, req: Any) -> None:
        """把先前缓存的图片补挂到本次请求上。

        `OnLLMRequestEvent` 早于 `prepare_request_images()`，所以这里改 `req.image_urls`
        能真正进入模型输入；晚于此再改就来不及了。
        """
        if self._cfg_int("image_bridge_ttl", 120) <= 0:
            return
        umo = getattr(event, "unified_msg_origin", None)
        if not self._has_cached_images(umo):
            return

        current = [str(x) for x in (getattr(req, "image_urls", None) or []) if x]
        cached = self._take_images(umo)
        merged = list(dict.fromkeys(current + [r for r in cached if r not in current]))
        if len(merged) <= len(current):
            return
        req.image_urls = merged
        added = len(merged) - len(current)
        event.set_extra("_switch_model_bridged_images", added)
        self.logger.info(
            "[自动切换模型] 已把先前缓存的 %d 张图片补挂到本次请求（本次共 %d 张）",
            added,
            len(merged),
        )

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
            "[自动切换模型] 已挂钩 %s 个 Provider 方法（%s 个类）。文本 %s 个 / 图像 %s 个 / 音频 %s 个，"
            "关键词 %s 条。",
            len(self._patched),
            len({c.__name__ for c, _, _ in self._patched}),
            len(self._list_of(MODE_TEXT)),
            len(self._list_of(MODE_IMAGE)),
            len(self._list_of(MODE_AUDIO)),
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
        hit, fragment = self._match(target)
        if hit:
            return True, fragment
        if self._is_err_response(target) and self._cfg_bool("match_response_text", True):
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
                if self._cfg_bool("auto_append_enabled", True):
                    known = set(ids)
                    ids.extend(sorted(pid for pid in self._chat_instances() if pid not in known))
            else:
                ids = sorted(self._chat_instances())
        else:
            # 图像 / 音频列表严格按手写顺序，不自动追加：能力未知，不敢替你猜
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
        items = self._as_id_list(self.config.get(key))
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

        self.config[key] = items
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

    @staticmethod
    def _mode_of_call(kwargs: dict, fallback: str) -> str:
        """从调用参数再确认一次类型（消息链判定为主，这里兜底）。"""
        if kwargs.get("audio_urls"):
            return MODE_AUDIO
        if kwargs.get("image_urls"):
            return MODE_IMAGE
        return fallback

    async def _chat_with_switch(self, provider: Any, orig: Any, args, kwargs):
        pid = self._pid(provider)
        if not (self._patched_ready or _HOOKS_INSTALLED):
            return await orig(provider, *args, **kwargs)

        # persist=False 是 AstrBot 内部辅助调用（图片转述、标题生成等），
        # 它们失败不该触发主模型切换。
        if kwargs.get("persist") is False:
            return await orig(provider, *args, **kwargs)

        mode = self._mode_of_call(kwargs, self._pending_mode())

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

        if kwargs.get("persist") is False:
            async for chunk in orig(provider, *args, **kwargs):
                yield chunk
            return

        mode = self._mode_of_call(kwargs, self._pending_mode())

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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("switchmodel")
    async def switchmodel_cmd(self, event: AstrMessageEvent, arg: str = ""):
        """模型切换插件管理指令。

        用法：/switchmodel [status|list|modes|reset|use|on|off|test]
        """
        parts = (arg or "").split()
        sub = parts[0].lower() if parts else "status"

        if sub in ("status", "状态"):
            yield event.plain_result(self._render_status())
        elif sub in ("list", "列表"):
            target = parts[1].lower() if len(parts) >= 2 else MODE_TEXT
            target = target if target in MODE_KEYS else MODE_TEXT
            yield event.plain_result(self._render_list(target))
        elif sub in ("modes", "类型"):
            yield event.plain_result(self._render_modes())
        elif sub in ("reset", "重置"):
            self._cooldown_until.clear()
            self._last_reason.clear()
            self._current_id = None
            self._preferred_id = None
            self._pending.clear()
            self._pending_any = None
            yield event.plain_result("已清空全部冷却状态与运行时偏好，主模型回到配置值。")
        elif sub in ("on", "off"):
            self.config["enabled"] = sub == "on"
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
                "用法：/switchmodel status|list [text|image|audio]|modes|reset|"
                "use <模型ID>|on|off|test [模型ID]"
            )

    def _render_status(self) -> str:
        mode = self._pending_mode()
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
            f"自动同步：{'开' if sync_on else '关'}（每 {sync_interval} 秒检查 AstrBot 模型设置）",
            f"持久化切换：{'开' if self._cfg_bool('persist_switch', True) else '关（仅本次运行有效）'}",
            "",
            f"文本列表：{len(self._list_of(MODE_TEXT))} 个"
            f"{'（未配置 → 全部已启用对话模型）' if not self._list_of(MODE_TEXT) else ''}",
            f"图像列表：{len(self._list_of(MODE_IMAGE))} 个"
            f"{'（未配置 → 图像请求不做特殊处理）' if not self._list_of(MODE_IMAGE) else ''}",
            f"音频列表：{len(self._list_of(MODE_AUDIO))} 个"
            f"{'（未配置 → 音频请求不做特殊处理）' if not self._list_of(MODE_AUDIO) else ''}",
            f"本次请求类型：{MODE_NAMES.get(mode, mode)}",
            "",
            f"运行时偏好：{self._preferred_id or '（未切换，跟随配置）'}",
            f"配置主模型：{self._config_provider_id() or '（未设置）'}",
            f"钩子状态：Provider {len(self._patched)} 个方法 / 解析拦截 "
            f"{'已启用' if self._resolve_async_orig else '未启用'}",
            f"累计切换：{self._switch_count} 次",
        ]
        if self._last_switch:
            ts, old, new, mode = self._last_switch
            lines.append(
                f"最近切换：{time.strftime('%H:%M:%S', time.localtime(ts))}  "
                f"{old} → {new}（{MODE_NAMES.get(mode, mode)}）"
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
        labels = {MODE_TEXT: "文本", MODE_IMAGE: "图像", MODE_AUDIO: "音频"}
        lines = [f"{labels[mode]}请求的候选顺序（* = 冷却中，× = 未安装）："]
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

    def _render_modes(self) -> str:
        instances = self._chat_instances()
        lines = ["三类模型的手动优先级（第一行先试，依次往后）："]
        for mode in (MODE_TEXT, MODE_IMAGE, MODE_AUDIO):
            items = self._list_of(mode)
            head = f"[{MODE_NAMES[mode]}] {MODE_KEYS[mode]}"
            if not items:
                fallback = "全部已启用对话模型" if mode == MODE_TEXT else "未配置 → 不干预"
                lines.append(f"{head}：空（{fallback}）")
                continue
            lines.append(f"{head}：")
            for idx, pid in enumerate(items, 1):
                mark = "" if pid in instances else "  × 未安装"
                lines.append(f"  {idx}. {pid}{mark}")
        lines.append("")
        lines.append("判定规则：消息里有音频 → 音频列表；否则有图片 → 图像列表；否则文本列表。")
        lines.append("图像/音频列表为空时不做特殊处理，交给 AstrBot 原逻辑。")
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
