"""本地单测：模型列表自动同步 + 失败处理策略。

不依赖 AstrBot 运行时：这里给 `astrbot.*` 注入最小 stub，再直接加载 main.py，
只测纯逻辑（配置读写、排序、计数、阈值），不碰真实的 Provider / 事件总线。

运行：
    python tests/test_sync_and_policy.py
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------- astrbot stub


def _decorator(*args, **kwargs):
    def wrap(fn):
        return fn

    return wrap


class _Star:
    def __init__(self, context=None, config=None):
        self.context = context
        self.config = config if config is not None else {}
        self.logger = logging.getLogger("stub")


class _ProviderType:
    CHAT_COMPLETION = types.SimpleNamespace(value="chat_completion")


class _FilterStub:
    """任何 `filter.xxx()` 都当作装饰器；两个枚举按需给值。"""

    PlatformAdapterType = types.SimpleNamespace(ALL="ALL")
    PermissionType = types.SimpleNamespace(ADMIN="ADMIN")

    def __getattr__(self, name):
        return _decorator


def _install_stubs() -> None:
    filter_stub = _FilterStub()

    def mod(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module

    astrbot = mod("astrbot")
    api = mod("astrbot.api")
    core = mod("astrbot.core")
    provider = mod("astrbot.core.provider")
    astrbot.api = api
    astrbot.core = core
    api.event = mod("astrbot.api.event", AstrMessageEvent=object, filter=filter_stub)
    api.star = mod("astrbot.api.star", Context=object, Star=_Star, register=_decorator)
    api.logger = mod("astrbot.api.logger", logger=logging.getLogger("stub"))
    core.provider = provider
    provider.entities = mod("astrbot.core.provider.entities", ProviderType=_ProviderType)
    provider.manager = mod("astrbot.core.provider.manager", ProviderManager=type("ProviderManager", (), {}))
    provider.provider = mod("astrbot.core.provider.provider", Provider=type("Provider", (), {}))


def _load_plugin_module():
    _install_stubs()
    spec = importlib.util.spec_from_file_location("switch_model_main", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["switch_model_main"] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin_module()


# ------------------------------------------------------------------- 测试夹具


class FakeConfig(dict):
    """带 save_config 的 dict，记录落盘次数。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_count = 0

    def save_config(self):
        self.save_count += 1


class FakeContext:
    def __init__(self, config: dict):
        self._config = config

    def get_config(self):
        return self._config


class FakeInstance:
    def __init__(self, provider_type=None):
        self.provider_config = {"provider_type": provider_type}


def make_plugin(config=None, instances=None, astrbot_cfg=None):
    """构造一个不跑 __init__ 的实例，只装配被测逻辑需要的最小状态。"""
    p = plugin.SwitchModelPlugin.__new__(plugin.SwitchModelPlugin)
    p.config = config if config is not None else FakeConfig()
    p.logger = logging.getLogger("test")
    p._cooldown_until = {}
    p._last_reason = {}
    p._failure_counts = {}
    p._self_removed = set()
    p._synced_providers = {}
    p._sync_task = None
    p._preferred_id = None
    p.context = FakeContext(astrbot_cfg or {})
    instances = instances if instances is not None else {}
    p._chat_instances = lambda: instances
    return p


TEXT_CFG = {"agent_runner": {"config": {"model": {"provider_id": "b"}}}}


# --------------------------------------------------------------- 同步到列表首位


class TestSyncProviderLists(unittest.TestCase):
    def test_promotes_existing_model_to_front(self):
        cfg = FakeConfig({"text_models": ["a", "b", "c"]})
        p = make_plugin(cfg, {"a": FakeInstance(), "b": FakeInstance(), "c": FakeInstance()}, TEXT_CFG)

        changed = p._sync_provider_lists(force=True)

        self.assertEqual(cfg["text_models"], ["b", "a", "c"])
        self.assertEqual(changed, ["text_models"])
        self.assertEqual(cfg.save_count, 1)

    def test_adds_model_not_in_list(self):
        cfg = FakeConfig({"text_models": ["a", "c"]})
        p = make_plugin(cfg, {"a": FakeInstance(), "b": FakeInstance(), "c": FakeInstance()}, TEXT_CFG)

        changed = p._sync_provider_lists(force=True)

        self.assertEqual(cfg["text_models"], ["b", "a", "c"])
        self.assertIn("text_models", changed)

    def test_noop_when_already_first(self):
        cfg = FakeConfig({"text_models": ["b", "a"]})
        p = make_plugin(cfg, {"a": FakeInstance(), "b": FakeInstance()}, TEXT_CFG)

        changed = p._sync_provider_lists(force=True)

        self.assertEqual(changed, [])
        self.assertEqual(cfg["text_models"], ["b", "a"])
        self.assertEqual(cfg.save_count, 0)

    def test_empty_list_is_left_alone(self):
        cfg = FakeConfig({"text_models": []})
        p = make_plugin(cfg, {"b": FakeInstance()}, TEXT_CFG)

        changed = p._sync_provider_lists(force=True)

        self.assertEqual(changed, [])
        self.assertEqual(cfg["text_models"], [])

    def test_non_chat_model_is_skipped(self):
        """语音识别模型（STT）不是对话模型，不能塞进 audio_models。"""
        cfg = FakeConfig({"audio_models": ["omni"]})
        astrbot_cfg = {"provider_stt_settings": {"provider_id": "sensevoice"}}
        p = make_plugin(
            cfg,
            {"omni": FakeInstance(), "sensevoice": FakeInstance("speech_to_text")},
            astrbot_cfg,
        )

        changed = p._sync_provider_lists(force=True)

        self.assertEqual(changed, [])
        self.assertEqual(cfg["audio_models"], ["omni"])

    def test_image_caption_model_syncs_to_image_list(self):
        cfg = FakeConfig({"image_models": ["old-vision"]})
        astrbot_cfg = {"provider_settings": {"default_image_caption_provider_id": "new-vision"}}
        p = make_plugin(
            cfg, {"old-vision": FakeInstance(), "new-vision": FakeInstance()}, astrbot_cfg
        )

        changed = p._sync_provider_lists(force=True)

        self.assertEqual(cfg["image_models"], ["new-vision", "old-vision"])
        self.assertEqual(changed, ["image_models"])

    def test_switch_can_be_disabled(self):
        cfg = FakeConfig({"text_models": ["a", "b"], "auto_sync_provider_models": False})
        p = make_plugin(cfg, {"a": FakeInstance(), "b": FakeInstance()}, TEXT_CFG)

        changed = p._sync_provider_lists(force=True)

        self.assertEqual(changed, [])
        self.assertEqual(cfg["text_models"], ["a", "b"])

    def test_unchanged_provider_is_not_rescanned(self):
        """同一个模型反复出现时不该反复落盘。"""
        cfg = FakeConfig({"text_models": ["a", "b"]})
        p = make_plugin(cfg, {"a": FakeInstance(), "b": FakeInstance()}, TEXT_CFG)

        p._sync_provider_lists(force=True)
        saves_after_first = cfg.save_count
        p._sync_provider_lists()  # 非 force：pid 没变，应直接返回

        self.assertEqual(cfg.save_count, saves_after_first)
        self.assertEqual(cfg["text_models"], ["b", "a"])

    def test_promoted_model_is_cleared_from_removed_set(self):
        """用户重新选回被移除的模型时，要解除移除状态。"""
        cfg = FakeConfig({"text_models": ["a", "b"]})
        p = make_plugin(cfg, {"a": FakeInstance(), "b": FakeInstance()}, TEXT_CFG)
        p._self_removed.add(("b", "text"))

        p._sync_provider_lists(force=True)

        self.assertNotIn(("b", "text"), p._self_removed)


# --------------------------------------------------------------- 失败处理策略


class TestFailurePolicy(unittest.TestCase):
    def _cfg(self, **extra):
        base = {"text_models": ["a", "b", "c"]}
        base.update(extra)
        return FakeConfig(base)

    def _instances(self):
        return {k: FakeInstance() for k in ("a", "b", "c")}

    def test_defaults_are_backward_compatible(self):
        p = make_plugin(self._cfg(), self._instances())
        self.assertEqual(p._failure_policy(), "demote")
        self.assertEqual(p._failure_threshold(), 3)

    def test_invalid_policy_falls_back_to_demote(self):
        p = make_plugin(self._cfg(failure_policy="whatever"), self._instances())
        self.assertEqual(p._failure_policy(), "demote")

    def test_none_policy_only_cools_down(self):
        cfg = self._cfg(failure_policy="none", failure_threshold=1)
        p = make_plugin(cfg, self._instances())

        p._mark_failed("a", "quota", "text")

        self.assertEqual(cfg["text_models"], ["a", "b", "c"])
        self.assertEqual(p._failure_counts, {})
        self.assertTrue(p._in_cooldown("a"))

    def test_below_threshold_keeps_order(self):
        cfg = self._cfg(failure_policy="demote", failure_threshold=3)
        p = make_plugin(cfg, self._instances())

        p._mark_failed("a", "quota", "text")
        p._mark_failed("a", "quota", "text")

        self.assertEqual(cfg["text_models"], ["a", "b", "c"])
        self.assertEqual(p._failure_counts[("a", "text")], 2)

    def test_demote_moves_model_to_end(self):
        cfg = self._cfg(failure_policy="demote", failure_threshold=2)
        p = make_plugin(cfg, self._instances())

        p._mark_failed("a", "quota", "text")
        p._mark_failed("a", "quota", "text")

        self.assertEqual(cfg["text_models"], ["b", "c", "a"])
        self.assertEqual(cfg.save_count, 1)
        self.assertEqual(p._failure_counts, {})

    def test_remove_drops_model_and_blocks_it(self):
        cfg = self._cfg(failure_policy="remove", failure_threshold=2)
        p = make_plugin(cfg, self._instances())

        p._mark_failed("a", "quota", "text")
        p._mark_failed("a", "quota", "text")

        self.assertEqual(cfg["text_models"], ["b", "c"])
        self.assertIn(("a", "text"), p._self_removed)
        self.assertEqual(p._ordered_ids("text"), ["b", "c"])

    def test_remove_skips_when_it_is_the_only_model(self):
        cfg = FakeConfig({"text_models": ["a"], "failure_policy": "remove", "failure_threshold": 1})
        p = make_plugin(cfg, {"a": FakeInstance()})

        p._mark_failed("a", "quota", "text")

        self.assertEqual(cfg["text_models"], ["a"])
        self.assertEqual(p._self_removed, set())

    def test_success_resets_counter(self):
        cfg = self._cfg(failure_policy="demote", failure_threshold=3)
        p = make_plugin(cfg, self._instances())

        p._mark_failed("a", "quota", "text")
        p._mark_failed("a", "quota", "text")
        p._mark_ok("a", "text")
        p._mark_failed("a", "quota", "text")

        self.assertEqual(p._failure_counts[("a", "text")], 1)
        self.assertEqual(cfg["text_models"], ["a", "b", "c"])

    def test_mark_ok_without_mode_clears_all_modes(self):
        p = make_plugin(self._cfg(), self._instances())
        p._failure_counts[("a", "text")] = 2
        p._failure_counts[("a", "image")] = 1

        p._mark_ok("a")

        self.assertEqual(p._failure_counts, {})

    def test_counters_are_isolated_per_mode(self):
        cfg = self._cfg(failure_policy="demote", failure_threshold=2)
        p = make_plugin(cfg, self._instances())

        p._mark_failed("a", "quota", "text")
        p._mark_failed("a", "quota", "image")  # 不同模式，不该合并

        self.assertEqual(cfg["text_models"], ["a", "b", "c"])
        self.assertEqual(p._failure_counts[("a", "text")], 1)
        self.assertEqual(p._failure_counts[("a", "image")], 1)

    def test_empty_list_only_cools_down(self):
        cfg = FakeConfig({"text_models": [], "failure_policy": "remove", "failure_threshold": 1})
        p = make_plugin(cfg, {"a": FakeInstance()})

        p._mark_failed("a", "quota", "text")

        self.assertEqual(cfg["text_models"], [])
        self.assertEqual(p._self_removed, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
