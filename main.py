"""astrbot_plugin_switch_model 1.6.0 —— 文本模型报错自动切换。

只保留文本对话模型的故障切换：
- 按 `text_models` 的顺序挑选本次使用的模型（列表留空则跟随 AstrBot 主模型）；
- 调用命中错误关键词（额度耗尽、限流、鉴权失败、模型下线等）时，按顺序往后换；
- 失败模型进入冷却期，冷却结束自动恢复候选资格；
- 切换成功后立即生效，可选落盘到 `agent_runner.config.model.provider_id`。

图像 / 音频的切换、图片补挂、可视化配置页均已移除，所有设置都在插件配置表单里。
插件只在内存中挂钩 Provider 层，卸载 / 重载后自动还原，不修改 AstrBot 源码。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import LLMResponse, ProviderType
from astrbot.core.provider.manager import ProviderManager
from astrbot.core.provider.provider import Provider

PLUGIN_NAME = "astrbot_plugin_switch_model"
VERSION = "1.6.0"

# 仅支持文本对话模型
MODE_TEXT = "text"
MODE_NAMES = {MODE_TEXT: "文本"}
MODE_KEYS = {MODE_TEXT: "text_models"}

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

        # 「text_models 首行覆盖了 WebUI 主模型」只提示一次，避免刷屏
        self._text_override_warned = False

    # ------------------------------------------------------------ 可视化配置接口


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
            items = self._as_id_list(self.config.get(key))
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

    def _apply_preferred(self, provider_type, resolved, umo: str | None = None):
        """解析拦截：让本轮选定的文本模型立即生效，无需保存或重启。"""
        value = getattr(provider_type, "value", str(provider_type))
        if value != ProviderType.CHAT_COMPLETION.value:
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

    def _list_of(self, mode: str = MODE_TEXT) -> list[str]:
        """取文本模型的手动优先级列表。"""
        items = self._as_id_list(self.config.get("text_models"))
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
        """请求发出前的最后一道加工：抑制正文里重复的 @。"""
        if req is None or not self._cfg_bool("enabled", True):
            return
        self._inject_no_text_mention(req)

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

        mode = MODE_TEXT

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

        mode = MODE_TEXT
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
