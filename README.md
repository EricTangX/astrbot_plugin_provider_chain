# 模型故障自动切换

**AstrBot 本地插件：报错自动切换 · 文本/图像双优先级 · 失败冷却降级 · 严格候选集 · 发图 @ 补挂。**



**2.0.0 功能完善**：支持图文分开发送，消息合并，图片补挂，图片解码等功能；设置页按「基础 / 切换规则 / 模型与同步 / 图片处理」四张卡片分区，枚举项（排序策略、匹配方式、失败策略）改为下拉选择。

**1.6.0 化繁为简**：只保留主要模型的故障自动切换功能，移除侧边菜单，降低消息的干扰，专注于模型的切换。

**1.5.0 新增两件事**：

1. **模型设置自动同步**——你在 AstrBot 里改了主对话模型、图片转述模型或语音模型，插件会自动把它提到对应列表的首位（已在列表里就上移，不在就新增），不必再回插件页手动添加。
2. **连续失败处理策略**——同一个模型连续失败达到阈值后，可按配置把它降到列表末尾（`demote`）或直接移出列表（`remove`），也可以关掉只保留冷却（`none`）。

**内核仍然是 1.0.0 的干净基座**：文本、图像、音频三类模型各有一份有序列表，你写什么顺序就按什么顺序来，插件不会自作主张替你推断模型能力。

---

## 版本演进

| 版本 | 主要内容 |
|---|---|
| 1.0.0 | 按列表顺序切换、关键词判定、失败冷却、解析拦截即时生效 |
| 1.1.0 | 拆成文本 / 图像 / 音频三份列表 + 消息分类钩子 |
| 1.2.0 | 插件更名为 `astrbot_plugin_switch_model` |
| 1.3.0 | 可视化配置页（`pages/model-priority`）、配置表单支持原生下拉选模 |
| 1.4.0 | 图片补挂（先发图再 @ 提问）、纯图片消息不抢答 |
| 1.4.1 | 抑制回复正文里重复出现的 @ |
| 1.5.0 | 模型设置自动同步、连续失败处理策略 |

## 一、1.1.0 相对 2.x 还原了什么

2.x 会自动读取每个模型的「模型能力（modalities）」，据此推断请求该路由到谁，还带了降级、报错、图片补挂一整套。这一版全部移除，只保留「按你写的顺序执行」。

**删掉的代码**

| 2.x 里的东西 | 处理 |
|---|---|
| `_provider_modalities()` / `_supports()` / `_modality_label()` | 删除。不再读取 `provider_config["modalities"]` |
| 自动模态路由决策（按能力挑模型） | 删除。改为「取对应列表的第一项」 |
| `modality_fallback` 降级/报错（strip / error） | 删除。列表为空时不干预，交给 AstrBot 原逻辑 |
| `on_llm_request` 钩子（请求成型后剔除内容、写系统提示） | 删除。不再改写请求 |
| `image_bridge` 图片补挂（跨消息挂图） | 删除 |
| `debug_request` 请求诊断日志 | 删除 |
| `modality_overrides` / `modality_auto_append` / `text_route_specialized` / `modality_ttl` | 删除这些配置项 |
| `priorities` 字典 + `strategy=priority` | 删除。优先级直接用列表顺序表达，不再有两套机制 |

**保留的 1.0.0 内核**

- 关键词判定（`error_keywords` + `match_mode` + `ignore_case` + `match_response_text`）
- 失败模型冷却（`cooldown_seconds`）
- 按顺序切换、单次最多试 `max_chain` 个
- 解析拦截（`ProviderManager.get_using_provider(_async)`），使切换**立即生效**，不需要保存或重启
- 切换成功后改写 `agent_runner.config.model.provider_id`，可选落盘（`persist_switch`）
- 指令：`status` / `list` / `reset` / `use` / `on` / `off` / `test`

**新增**

- `text_models` / `image_models` / `audio_models` 三份有序列表
- 消息分类钩子：收到消息时判断带的是图片还是音频，决定用哪份列表
- `/switchmodel modes`、`/switchmodel list [text|image|audio]`

---

## 二、目录结构

```
astrbot_plugin_switch_model/
├── main.py             # 插件主体（Provider 挂钩 + 分类钩子 + 模型同步 + 失败策略 + 页面接口 + 指令）
├── pages/
│   └── model-priority/
│       └── index.html  # 可视化配置页：下拉选择 + 拖动排序
├── tests/
│   └── test_sync_and_policy.py  # 本地单测：同步与失败策略（stub 注入，无需 AstrBot 运行时）
├── metadata.yaml       # 插件元数据（名称/版本/简介）
├── _conf_schema.json   # 配置项定义，决定 WebUI 里的配置表单
├── README.md           # 本文件
└── requirements.txt    # 依赖（无第三方依赖）
```

> 跑单测：`python tests/test_sync_and_policy.py`（20 个用例，覆盖同步的 8 种情形与失败策略的 12 种情形）。

> `pages/` 是 AstrBot 的插件页面约定：每个子目录一个页面，入口固定为 `index.html`，
> 目录名即页面名。页面通过 `/api/plugin/page/bridge-sdk.js` 与主程序通信，
> 后端接口由 `context.register_web_api()` 注册在 `/api/plug/astrbot_plugin_switch_model/models`。

## 三、安装

### 方式一：WebUI 从文件安装（推荐）

1. 得到 `astrbot_plugin_switch_model.zip`（包内为单层 `astrbot_plugin_switch_model/` 目录）；
2. WebUI → 插件管理 → 右上角「+」→「安装插件」→「从文件安装」→ 选择该 zip；
3. 安装后自动加载，首次加载生成 `data/config/astrbot_plugin_switch_model_config.json`。

### 方式二：手动放置目录

1. 把整个 `astrbot_plugin_switch_model` 目录放到 `<AstrBot 数据目录>\plugins\astrbot_plugin_switch_model`；
2. 重启 AstrBot，或进入 WebUI「插件管理」点重载。

> 无第三方依赖，不需要额外 `pip install`。

### 从 1.2.0 及更早版本升级（插件改名）

1.3.0 起插件标识从 `SwitchModel` 改为 `astrbot_plugin_switch_model`（符合市场命名约定）。AstrBot 的插件配置文件名跟着**目录名**走，所以升级要搬两样东西：

```text
data/plugins/SwitchModel/                  → data/plugins/astrbot_plugin_switch_model/
data/config/SwitchModel_config.json        → data/config/astrbot_plugin_switch_model_config.json
```

旧目录务必删除——同一份 metadata 存在两个目录会导致重复注册。配置改完名后三份列表原样保留，不需要重新填写。

---

## 四、工作流程

```
收到消息
   │
   ├─ 有音频（语音/音频文件）→ 记为「音频请求」
   ├─ 有图片（图片/图片文件）→ 记为「图像请求」
   └─ 都没有 ────────────────→ 记为「文本请求」
   │
   ▼
解析 Provider 时
   │
   ├─ 图像请求且 image_models 非空 → 取该列表第一个可用模型
   ├─ 音频请求且 audio_models 非空 → 取该列表第一个可用模型
   └─ 其余（文本请求）
        ├─ 有过运行时偏好（刚切换成功）→ 用偏好的模型
        ├─ text_models 非空 ─────────→ 取第一行
        └─ text_models 为空 ─────────→ 沿用 WebUI 主模型
   │
   ▼
调用模型
   │
   ├─ 正常返回 → 结束
   └─ 报错且命中关键词
        │
        ├─ 当前模型进入冷却（默认 180s）
        ├─ 在同一份列表里按顺序往后试（跳过冷却中/未安装）
        ├─ 成功 → 返回结果
        │    · 文本请求：改写主模型，后续请求都走它（可落盘）
        │    · 图像/音频请求：只本次生效，不动主模型
        └─ 全部失败 → 记 ERROR 日志，抛原始异常
```

---

## 五、配置项说明

配置文件：`data/config/astrbot_plugin_switch_model_config.json`
配置入口：WebUI → 插件管理 → 自动切换模型 → 配置（保存后立即生效，无需重启）

### 三类优先级（本次新增，核心）

| 参数 | 类型 | 默认值 | 作用 |
|---|---|---|---|
| `text_models` | list | `[]` | **纯文本请求**的优先级。第一项就是首选，报错依次往后。留空 = 沿用 WebUI 主模型 |
| `image_models` | list | `[]` | **带图片的请求**的优先级。请只选能看图的模型。留空 = 不干预，交给 AstrBot 原逻辑 |
| `audio_models` | list | `[]` | **带音频的请求**的优先级。请只选能听音频的对话模型。留空 = 不干预 |

示例（把顺序换成你自己想要的即可）：

```json
"text_models":  ["阿里云百炼/glm-5.3", "阿里云百炼/kimi-k3", "zhipu/GLM-4.5-Flash"],
"image_models": ["阿里云百炼/qwen3.8-max", "阿里云百炼/qwen3.8-omni-flash"],
"audio_models": ["阿里云百炼/qwen3.8-omni-flash"]
```

`provider_id` 就是「模型提供商」里显示的条目 ID，形如 `阿里云百炼/glm-5.3`。填错不会报错，只会在 `/switchmodel list image` 里标为 `×`（未安装）。

> **这三项在配置表单里是下拉选择器**：点输入框弹出搜索列表，勾选即加入；已选区域每项右侧有 `↑` `↓` 调整先后、`✕` 移除，**列表顺序就是优先级顺序**。选项来自当前**已启用**的提供商（只列对话模型，TTS / STT / 向量 / 重排不会出现）。
>
> 想按模态筛选、看能力徽标和冷却状态，用插件页面 `model-priority`（见第六节）。两种入口读写同一份 `list[str]`，可以混用。

> **关于 WebUI 主模型和 `text_models` 的关系**
>
> 两者都表示「文本请求用哪个模型」，所以必须定一条优先级规则：
>
> - `text_models` **留空** → 用 WebUI → 配置 → 其他配置里的「主模型」，行为与没装插件时完全一致；
> - `text_models` **非空** → 文本请求以**第一行**为准，WebUI 主模型不再决定实际调用。
>
> 这样三份列表的语义才一致（第一行 = 首选）。插件在切换成功时会同步改写 `provider_id` 并（默认）落盘，所以两边不会长期不一致；如果发现不一致，以 `text_models` 为准，并会在日志里提示一次。

### 分流与排序规则

1. **分类只看消息里有没有音视频内容**：`Record` → 音频；`Image` → 图像；`File` 按扩展名（`.png/.jpg/...` → 图像，`.amr/.silk/.mp3/...` → 音频）。
2. **音频优先**：一条消息同时有图和语音时按音频请求处理（音频模型更稀缺，通常也具备看图能力）。
3. **列表就是优先级**：`strategy=order` 时严格按你写的顺序；`alphabetical` 时改成按 ID 字典序（一般用不到）。
4. **图像/音频列表不自动追加**：只在你写进去的模型里选，因为插件不读模型能力，不敢替你猜谁能看图。
5. **文本列表会自动补**：`auto_append_enabled=true` 时，未列出的已启用对话模型按 ID 排序补在队尾，避免漏配导致无候选。
6. **判定按会话隔离**：类型判定记在「会话 + 到期时间」上。A 群刚发过一张图，不会把 B 群同一时间的纯文本请求也带到视觉模型上；只有拿不到会话标识的内部调用（图片转述、标题生成等）才沿用「最近一次判定」，且同样受 `mode_ttl` 限制。

> 第 6 条是 1.1.0 相对早期实现的一处修正：早期版本在「知道会话、但该会话没有记录」时会回落到全局最近判定，导致一次图片消息在 `mode_ttl` 秒内污染其它会话的文本请求。
7. **文本列表非空时，第一行即首选**：在解析阶段直接采用，不必等报错；留空则跟随 WebUI 主模型。

### 故障切换

| 参数 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `enabled` | bool | `true` | 总开关，关闭后不挂钩任何调用 |
| `error_keywords` | list | 18 条常见错误 | 命中任一关键词即触发切换 |
| `match_mode` | string | `substring` | `substring` 子串包含 / `regex` 正则 |
| `ignore_case` | bool | `true` | 关键词匹配忽略大小写 |
| `match_response_text` | bool | `true` | 除异常外，也检查 `role=err` 的返回文本 |
| `strategy` | string | `order` | `order` 按列表 / `alphabetical` 按 ID |
| `auto_append_enabled` | bool | `true` | 仅对 `text_models` 生效 |
| `max_chain` | int | `4` | 单次报错最多连续尝试几个模型 |
| `cooldown_seconds` | int | `180` | 失败模型冷却时长 |
| `failure_policy` | string | `demote` | 连续失败达阈值后：`none` / `demote` / `remove` |
| `failure_threshold` | int | `3` | 触发处理策略所需的连续失败次数（1~20） |
| `auto_sync_provider_models` | bool | `true` | 用户改 AstrBot 模型设置时自动同步到列表首位 |
| `auto_sync_interval_seconds` | int | `20` | 同步检查间隔（秒，5~600） |
| `persist_switch` | bool | `true` | 文本请求切换后是否落盘 |
| `warn_when_empty` | bool | `true` | 无候选时是否打 ERROR 日志 |
| `mode_ttl` | int | `30` | 单条消息的「类型判定」保留多久（秒），到期即回落为文本 |

### 模型设置自动同步（1.5.0 新增）

以前改完主模型，还得回插件页把新模型手动拖到列表首位，否则插件仍按旧列表选模。现在不需要了：

| AstrBot 里的设置 | 同步到 |
|---|---|
| `agent_runner.config.model.provider_id`（主对话模型） | `text_models` |
| `provider_settings.default_image_caption_provider_id`（图片转述模型） | `image_models` |
| `provider_stt_settings.provider_id`（语音识别模型） | `audio_models` |

规则：

1. **只提优先级，不删东西**：模型已在列表里就移到首位；不在就插到首位。原列表中其它模型的相对顺序保持不变。
2. **只在列表非空时同步**：空列表本身就等于「跟随 AstrBot 默认」，天然就是首选，写进去反而会限制候选范围。
3. **非对话模型跳过**：语音识别常是 `sensevoice` 这类 STT 模型，不是对话模型，塞进 `audio_models` 会导致请求失败，因此只记一条 INFO 日志后跳过。
4. **不重复落盘**：记住每个模式上次同步过的模型 ID，值没变就不读配置、不写配置。
5. **用户重新选中被移除的模型时自动解禁**：解除 `remove` 策略留下的运行时黑名单。

插件加载完成时同步一次，之后由后台任务按 `auto_sync_interval_seconds` 轮询。

### 连续失败处理策略（1.5.0 新增）

冷却解决的是「短时间内别反复撞同一个模型」，但一个额度已经耗尽的模型冷却结束后还会回来。新策略在**连续失败达到阈值**时永久降低它的位置：

| 策略 | 行为 |
|---|---|
| `none` | 只冷却，不改列表（1.4.x 及以前的行为） |
| `demote`（默认） | 移到该列表末尾，仍参与切换，但排最后 |
| `remove` | 从列表移除，并在**本次运行内**不再作为候选（重启后恢复） |

要点：

- **按模式分别计数**：`(模型, 文本/图像/音频)` 各记一份，图片请求失败不会影响文本列表的排序。
- **连续才计数**：中间成功一次即清零，避免长期累计把偶发错误算成故障。
- **`remove` 真的会挡住自动追加**：文本列表有 `auto_append_enabled`，仅删配置项会被下一轮补回队尾，所以额外维护一个运行时排除集合。
- **不会把列表清空**：只剩一个模型时跳过移除——列表变空会让插件退回「全部模型都参与」，等于没移除。
- **列表为空时只冷却**：空列表代表跟随默认，没有顺序可调。


### 关于「立即生效」

切换成功后分三步，**不需要手动保存、不需要重启**：

1. **内存偏好**：文本请求的新模型记为运行时首选，并接管 `ProviderManager.get_using_provider(_async)` 的解析结果——后续请求直接拿到新模型实例；
2. **内存配置**：就地改写 `agent_runner.config.model.provider_id`（同一个 `AstrBotConfig` 对象，立即对读取方可见）；
3. **落盘**：仅当 `persist_switch=true` 时调用 `save_config()`，目的是重启后仍保持。

所以 `persist_switch` 只决定「重启后还在不在」，**不影响当前是否立即生效**。

### 关于冷却

判定失败的模型在 `cooldown_seconds` 内不再参与候选，避免「主模型额度耗尽 → 每条消息都先撞一次 403」。冷却结束后自动恢复资格，无需重启。

### 关于图像/音频请求的切换

图像 / 音频请求的切换**只在本次生效**：不写运行时偏好、不改 `provider_id`、不落盘。原因是一次看图失败不应该把后续所有闲聊都拉到更贵的多模态模型上；那个失败的模型已经进了冷却，下次带图请求会自动跳过它。

### 「先发图、再 @ 提问」怎么处理（1.4.0 新增）

QQ 不允许在同一条消息里同时发 `@` 和图片，所以只能分两条发。但 AstrBot 有个结构性限制：

> `req.image_urls` 只从**当前这条事件**取图，图片也**不会写进对话历史**。因此那条 `@bot 这是谁` 的请求里一张图都没有，模型只能回「图片是空的」。

插件为此加了两件事，默认都开启：

| 参数 | 默认 | 作用 |
|---|---|---|
| `image_wait_followup` | `true` | 收到「只有图、没有文字也没有 @」的消息时先不回复，把图缓存下来等你追问，避免机器人对每张图自顾自评论 |
| `image_bridge_ttl` | `120` | 缓存图片的有效期（秒）。发完图后 2 分钟内提问都能把图带上；填 `0` 关闭补挂 |

工作方式：

1. **旁路观察**：在 `EventBus._print_event` 上挂一个只读观察器，每条入站消息只要有 `Image` 组件就先记下引用（本地路径优先，其次 `file`，最后 `url`）。这个位置早于所有管线，即使别的插件把消息 `stop_event()` 掉了也照样记得住；它**不参与唤醒判定**，不会让所有群消息都变成「需要响应」。
2. **纯图不抢答**：`on_waiting_llm_request` 里判定为「图片 + 无文本 + 无 @ + 无引用」时 `stop_event()`，图留在缓存里。
3. **补挂**：下一条同会话的 LLM 请求触发 `on_llm_request` —— 这个钩子**早于** `prepare_request_images()` —— 把缓存里的图追加进 `req.image_urls`，模型这次就真能看见图了。补挂是一次性的，成功后立即清空缓存，不会让后续每条消息都重复带图。
4. **选模同步修正**：只要本会话有未过期的缓存图，即使当前事件是纯文本，也按 `image_models` 选模型；否则会挑到不支持 `image` 的模型（比如 `glm-5.3`），AstrBot 会按模态把图裁掉，症状还是「图片是空的」。

配套修掉的一个判定 bug：`_remember_mode()` 原来是无条件覆盖，图片先记录、14 秒后一条文本就把记录盖成 `text`。现在图像 / 音频判定在有效期内不会被后到的纯文本覆盖。

> 图片只在内存里保存引用，不下载、不落盘，最多保留最近 3 张。QQ 图片链接有有效期，太久远的图即使还在缓存里也可能取不到，此时 AstrBot 会跳过它并在请求里给出提示。

### 回复里重复出现 2~3 个 @（1.4.1 新增）

现象：群里回复变成「@某人 @某人 @某人 正文」。

原因是两处 @ 叠加，**不是消息链里插了多个 At**（实测 635 条发送记录，chain 里从来没出现两个 `[At]`）：

1. `platform_settings.reply_with_mention = true` → AstrBot 在 `ResultDecorateStage` 里插入 1 个 `At` 组件；
2. 上下文里存在 `@昵称(QQ号)` 这种文本（AstrBot 把 At 序列化进历史、以及「只 @ 不说话」的唤醒提示），模型照着模仿，在正文开头又写一遍，偶尔写两遍。

插件在 `on_llm_request`（早于组装最终请求）里给 `system_prompt` 追加一句约束：

> 【回复格式】不要在正文里写 @昵称、@昵称(QQ号) 这类提及：平台会自动 @ 提问者，你再写一遍会在客户端显示成重复的 @。要提到某个人时直接写名字，不要带 @。

| 参数 | 默认 | 作用 |
|---|---|---|
| `suppress_text_mention` | `true` | 是否注入这条约束。关掉则维持原状 |

注意这是**提示词层面的约束**，模型绝大多数情况会遵守，但不是硬保证；历史污染特别重的会话里可能仍偶发。要 100% 消除，得在 `on_decorating_result` 里清洗正文（本插件未做，避免误删真正想 @ 别人的内容）。

---

## 六、两份配置入口：配置表单（1.3.0 起原生下拉）与插件页面

**入口一（日常够用）：插件配置表单**

WebUI → 插件管理 → 自动切换模型 → `⚙` 配置。三项优先级各是一个**下拉选择器**：

| 交互 | 说明 |
|---|---|
| 展开 | 点输入框弹出列表，带搜索框，还支持按提供商来源筛选 |
| 选项来源 | 当前**已启用**的提供商，只列对话模型；TTS / STT / 向量 / 重排不会混进来 |
| 勾选 | 点条目即加入，再点一次取消；选中项显示在「已选择 N 个模型」区域 |
| 排序 | 每项右侧 `↑` `↓`，调整后的顺序就是优先级顺序 |
| 移除 | 每项右侧 `✕` |
| 保存 | 与其它配置项一起「保存并关闭」，写回同一份 `list[str]`，立即生效 |

实现方式是把三项的 schema 标成 `"type": "list"` + `"_special": "select_providers"`，用 AstrBot 前端内置的提供商选择器渲染——**数据格式和之前完全一样**，所以旧配置无需迁移；在更老、不认识 `_special` 的版本上会自动退回原来的文本列表编辑器，不会坏。

**入口二（进阶）：插件页面 `model-priority`**

入口：WebUI → 插件管理 → 自动切换模型 → 页面 → `model-priority`（侧边栏也会出现同名入口）

表单里的下拉列出的是全部对话模型，无法按模态筛选；页面补上这部分能力，**配置文件格式仍是 `list[str]`，两种入口可以混用**：

| 能力 | 说明 |
|---|---|
| 下拉选择 | 选项来自当前**已启用的对话模型**，按类型分组：先「支持该类型（推荐）」，再「未声明支持该类型（谨慎添加）」 |
| 排序 | 拖动行，或点 `↑` / `↓`；保存后的顺序就是优先级顺序 |
| 即时校验 | 重复项自动去重并提示；不存在的模型、TTS/STT/embedding 这类非对话模型，后端直接拒绝并说明原因 |
| 能力提醒 | 往图像/音频列表里放没有声明对应模态的模型时，保存成功但会列出提醒，不阻止 |
| 状态可见 | 每行显示模态徽标、冷却剩余秒数、是否当前主模型；底部显示策略、主模型、自动补位、单次最多尝试数 |
| 反馈 | 有未保存修改时显示标记并拦截关闭页面；保存中禁用按钮；顶部横幅显示错误/提醒，底部 toast 显示结果 |

保存做了什么：

1. 校验通过后写回 `self.config` 的 `text_models` / `image_models` / `audio_models`（仍为字符串数组）；
2. 顺手删除 1.0.0 遗留的 `models` 键，避免它继续干扰文本列表；
3. 清空运行时偏好 `_preferred_id`——否则上一次故障切换留下的模型会继续压过新配置的第一行；
4. 调用 `save_config()` 落盘，然后返回最新状态，**无需重启**。

接口（普通用户不需要直接调用）：

```
GET  /api/plug/astrbot_plugin_switch_model/models   → {config, options, meta}
POST /api/plug/astrbot_plugin_switch_model/models   → 请求体为 {text_models, image_models, audio_models}
```

两个接口都走 WebUI 登录态鉴权；`POST` 失败时返回 `status=error`，`message` 里是可直接展示的中文原因。

---

## 七、指令

需要管理员权限：

| 指令 | 作用 |
|---|---|
| `/switchmodel` 或 `/switchmodel status` | 版本、启用状态、排序策略、失败策略与阈值、自动同步间隔、三份列表各有几个模型、本次请求类型、累计切换次数、最近一次切换、冷却中的模型 |
| `/switchmodel list [text\|image\|audio]` | 打印某一类的候选顺序（默认 `text`），`*`=冷却中，`×`=未安装 |
| `/switchmodel modes` | 一次性列出三份列表的完整内容与判定规则 |
| `/switchmodel reset` | 清空冷却状态与运行时偏好 |
| `/switchmodel use <provider_id>` | 手动指定主模型（作用于文本链路）并持久化 |
| `/switchmodel on` / `off` | 运行时开关（重启后以配置文件为准） |
| `/switchmodel test [provider_id]` | 对该模型发一次 `ping`，验证关键词判定与切换链路是否通畅 |

---

## 八、常见问题

**Q：为什么图像/音频列表留空时什么都不做？**
因为这一版不读模型能力，插件没有依据判断「谁该处理这张图」。留空时相当于把控制权交回 AstrBot：它会按当前模型的 `modalities` 决定保留还是丢弃图片。想让图片一定被看到，就把能看图的模型填进 `image_models`。

**Q：填在 `image_models` 里的模型其实看不了图会怎样？**
请求会真的发给它。AstrBot 在调用前会按该模型声明的 `modalities` 清洗上下文，所以要么它自己支持图片，要么图片被丢掉——插件不介入。配置前建议直接发一张图试，或用 `/switchmodel test <模型ID>` 验证连通性。

**Q：图片和文字被 QQ 拆成两条消息，还能识别吗？**
1.4.0 起可以。开启 `image_wait_followup`（默认开）时，纯图消息不立即回复，图片引用先缓存 `image_bridge_ttl` 秒；你接着 @ 提问时，插件在 `on_llm_request` 里把缓存图补进 `req.image_urls`。限制：只在内存里存引用、最多 3 张，超时或重启后失效。

**Q：我在 AstrBot 里改了主模型，为什么插件列表没变？**
三种情况都是正常的：① 新模型本来就在列表首位；② 该列表为空——空列表代表「跟随 AstrBot 默认」，本身就是首选，插件不会去写它；③ 关掉了 `auto_sync_provider_models`。另外语音识别模型常是 `sensevoice` 这类 STT，不是对话模型，会被跳过并打一条 INFO 日志。

**Q：`remove` 掉的模型怎么又回到候选里了？**
`remove` 的移除只保证**本次运行内**生效，重启插件后恢复。另外，如果你在 AstrBot 里重新选中它作为主模型，同步逻辑会把它重新提到首位并解除移除状态。

**Q：和 AstrBot 自带的 `fallback_provider_ids` 冲突吗？**
不冲突。自带兜底在 Agent Runner 层，本插件在 Provider 层与解析层。插件先切换成功，Runner 就认为主模型成功；插件全部失败才会把异常抛回给 Runner。

**Q：会修改 AstrBot 源码吗？**
不会。只在运行时 `setattr` 替换 Provider 子类的方法、包装 `ProviderManager` 的解析方法，插件卸载 / 重载时在 `terminate()` 里还原。

**Q：影响流式输出吗？**
不影响。`text_chat_stream` 单独挂钩：只要已经开始吐字就不再切换，只在「一个字都没出」时切换。

**Q：为什么有的错误不切换？**
没命中关键词的错误会原样抛出——参数错误、内容审核这类问题换模型也解决不了，盲目切换只会掩盖问题。需要的话把关键词加进 `error_keywords`。

**Q：`persist=False` 的内部调用（图片转述、标题生成）会被接管吗？**
不会。这类调用直接放行，避免一次转述失败就把主模型换掉。

---

## 九、版本记录

| 版本 | 变更 |
|---|---|
| 1.0.0 | 故障切换：关键词判定、顺序切换、冷却、解析拦截即时生效、落盘持久化、管理指令 |
| 2.0.x | （已回退）自动读取模型能力做模态路由、降级/报错、图片补挂、请求诊断 |
| 1.1.0 | 代码基座回到 1.0.0；新增 `text_models` / `image_models` / `audio_models` 三份手动优先级列表与消息分类；新增 `modes` 指令与 `list [类型]` 参数；删除 2.x 的能力嗅探、降级/报错、图片补挂、诊断日志 |
| 1.2.0 | 新增可视化配置页 `pages/model-priority`：三份列表改为「下拉选模型 + 拖动排序」，选项来自已启用的对话模型并按类型分组；新增 `GET/POST /api/plug/astrbot_plugin_switch_model/models` 接口，带重复/未知模型/非对话模型的校验与能力提醒；保存时清理旧 `models` 键并重置运行时偏好，改完立即生效。配置格式不变，仍为 `list[str]` |
| 1.3.0 | 插件标识改为 `astrbot_plugin_switch_model`（配合插件市场命名约定），配置文件名随之变为 `astrbot_plugin_switch_model_config.json`；三项优先级在**配置表单里变成原生下拉选择器**（`_special: select_providers`，勾选 + `↑↓` 排序，数据仍是 `list[str]`），插件页面保留为按模态分组/校验的进阶入口；补齐 `metadata.yaml` 的 `repo` / `author` / `tags` / `support_platforms` 以便发布到插件市场；显示名改为「自动切换模型」 |
| 1.4.0 | 修复 QQ「@ 和图片不能同条发送」导致的「图片为空」：新增图片补挂（`image_bridge_ttl` 默认 120 秒）+ 纯图不抢答（`image_wait_followup`）、事件总线旁路观察入站图片、`on_llm_request` 把图追加进 `req.image_urls`；修正 `_remember_mode` 让图像/音频判定不被后到的纯文本覆盖；有缓存图时强制按 `image_models` 选模，避免不支持 image 的模型把图裁掉 |
| 1.4.1 | 抑制回复正文里重复出现的 @：AstrBot 自动加的 `At` 组件与模型从上下文学来的 `@昵称(QQ号)` 文本叠加，导致显示 2~3 个 @；在 `on_llm_request` 里给 `system_prompt` 追加「正文不要写 @」约束（`suppress_text_mention`，默认开启），并将图片补挂逻辑拆到独立方法，避免关掉补挂时规则也不注入 |
| 1.5.0 | 新增两项能力。① **模型设置自动同步**：轮询 `agent_runner.config.model.provider_id`、`provider_settings.default_image_caption_provider_id`、`provider_stt_settings.provider_id`，用户改了就把新模型提到对应列表首位（已有则上移、没有则插入）；只在列表非空时生效，非对话模型跳过，值没变不落盘，用户重新选中被移除的模型时自动解禁。② **连续失败处理策略**：`failure_policy` 支持 `none` / `demote`（默认）/ `remove`，`failure_threshold` 控制阈值（默认 3）；按 `(模型, 模式)` 分别计数、成功即清零；`remove` 额外维护运行时黑名单，防止文本列表的 `auto_append_enabled` 把它补回队尾；只剩一个模型时跳过移除，避免列表变空退回全量候选。附 20 个本地单测 |
