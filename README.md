# MiMo TTS 装饰器 v0.6.2

这个版本重点做了 6 件事：

- 默认把临时 wav 放到 AstrBot 的 `data/plugin_data/{plugin_name}/temp`
- 新增临时 wav 自动回收，避免机器人长期运行后持续占磁盘
- `replace_plain` 模式下保留原消息链里的 `At` / `Reply`
- 新增 `auto_tag_density`，让自动打标更接近 MiMo 官方示例的标签密度
- 保留并延续 0.5.x 的自动打标、style、语速和朗读清洗能力
- 补充 `ruff` / `pyproject.toml` / `.gitignore`，更适合提交和发布

---

## 一、插件在做什么

MiMo 官方语音合成接口不是直接喂“纯文本”就完事，而是更接近下面这种结构：

- `user`：旁带说明，用来告诉 MiMo“怎么处理”
- `assistant`：真正要被合成的目标文本
- `audio.voice`：底层音色
- `assistant` 文本开头可带 `<style>...</style>` 控制整体风格
- 正文里还能加括号类音频标签做更细的节奏 / 力度 / 情绪控制

本插件就是把 AstrBot 原本要发出去的文字，自动组装成适合 MiMo TTS 的这套结构，再拿回 wav 发出去。

---

## 二、现在推荐的控制思路

### 1. `voice` 只负责选一个基础声线底色

当前官方公开可用值通常就是：

- `default_zh`
- `mimo_default`
- `default_en`

中文主场景建议优先用 `default_zh`。

### 2. `<style>` 负责“大风格”

推荐把这些整体风格放进 `global_style + speed_style`：

- 情绪变化：`开心 / 悲伤 / 生气`
- 角色扮演：`孙悟空 / 林黛玉`
- 风格变化：`俏皮 / 夹子音 / 台湾腔`
- 方言：`东北话 / 四川话 / 河南话 / 粤语`
- 语速：`稍快 / 变快 / 稍慢 / 变慢`

例如：

```text
<style>俏皮,夹子音,稍快</style>
```

### 3. 括号标签只负责“局部动作 / 状态 / 节奏”

这版开始，插件默认更推荐、更接近官方示例的标签风格：

- `（停顿）`
- `（小声）`
- `（沉默片刻）`
- `（长叹一口气）`
- `（语速加快）`
- `（苦笑）`
- `（咳嗽）`
- `（提高音量喊话）`
- `（紧张，深呼吸）`

不再推荐把下面这类抽象词直接塞进正文标签里：

- `（轻快）`
- `（轻盈灵动）`
- `（带一点撒娇）`
- `（元气一点）`
- `（句尾上扬）`

这些更适合放进 `<style>` 或放进 LLM 的幕后约束，而不是直接让 TTS 有机会把它们读出来。

---

## 三、配置项怎么理解

### 1. 全局风格 `global_style`

会自动包装成：

```text
<style>俏皮,夹子音</style>
```

再放到 `assistant` 文本最前面。

### 2. 语速控制 `speed_style`

会和 `global_style` 合并进同一个 `<style>` 标签。

例如：

- `global_style = 俏皮,夹子音`
- `speed_style = 稍快`

最终变成：

```text
<style>俏皮,夹子音,稍快</style>
```

### 3. 手工音频标签前缀 / 后缀

这是原样插到正文里的，适合临时实验。长期正式使用时，建议：

- 整体风格优先交给 `<style>`
- 括号标签只做局部动作/状态

### 4. `dummy_user_prompt` / “发送给 MiMo 的 user 旁带提示”

它不会被念出来，而是给 MiMo 的“幕后说明”。

作用是告诉 MiMo：

- 真正要被合成的是 `assistant` 里的文本
- 不要改写 `assistant` 文本
- 尽量用自然、清晰、稳定的普通话来读

按官方思路：

- 真正要读的内容放 `assistant`
- `user` 可以放背景说明，也可以省略

### 5. 临时音频目录与回收

- `temp_dir` 留空时，会自动使用 AstrBot 的 `data/plugin_data/{plugin_name}/temp`
- 每次发送成功后，插件会自动删除本次生成的临时 wav
- `temp_file_retention_hours` 用来在插件启动时清理历史残留文件

如果你是从旧版本升级上来的，旧默认目录 `/AstrBot/data/temp/mimo_tts` 也会自动迁移到新的默认目录逻辑

---

## 四、规则模式风格预设（更像官方打标示例）

### `catgirl_soft`

目标：软萌、陪伴感、轻可爱。

倾向：

- `（停顿）`
- `（沉默片刻）`
- `（小声）`

### `catgirl_energetic`

目标：元气、轻快、活泼。

倾向：

- `（语速加快）`
- `（提高音量喊话）`

### `gentle`

目标：温柔、轻柔、安抚。

倾向：

- `（停顿）`
- `（小声）`
- `（长叹一口气）`

### `neutral`

目标：最克制，只做基础润色。

倾向：

- `（停顿）`
- 少量节奏标签

---

## 五、自动标签两种模式怎么选

### `rule_based`

优点：

- 速度快
- 稳定
- 没额外模型成本
- 现在更像官方示例的打标方式

### `llm`

优点：

- 更细腻
- 更适合按句意做断句和口语化
- 情绪通常更贴近“想要的感觉”
- 更容易做出接近 MiMo 官方示例那种按句分布的标签密度

缺点：

- 依赖额外模型
- 可能偶尔跑偏

默认推荐：

- `tagger_model = gemini-3-flash-preview`
- `tagger_temperature = 0.3`
- `tagger_timeout_seconds = 45`

如果 `tagger_api_key`、真实 `tagger_base_url`、`tagger_model` 没配完整，插件会自动回退到规则模式，不会直接把消息搞坏。

### “更严格的 LLM 打标约束”

这是这版新增的关键开关。

开启后，会额外要求 LLM：

- 不要自由扩写
- 不要写测试说明/前言/总结
- 标签尽量模仿官方动作 / 状态 / 节奏示例
- 不要输出 `（轻快）` / `（轻盈灵动）` 这类容易被直接念出来的抽象标签

如果你追求：

- 口语化还在
- 但不要自由发挥太多

建议保持开启。

同时，`tagger_system_prompt` 仍然可以自定义；如果你自己填了提示词，插件会在后面再附加这一段更严格的约束。

### 自动标签密度 `auto_tag_density`

- `conservative`：标签更少，整体更克制
- `balanced`：更接近 MiMo 官方示例里那种按句分布的标签密度
- `aggressive`：更积极插入标签，表现力更强

如果你觉得现在“味道不够”，优先把它调到 `balanced` 或 `aggressive`，再观察日志里的 `llm tagged text`。

---

## 六、朗读友好清洗

这个开关是独立的：`speech_cleanup_enabled`

开启后会清理：

- `@某人`
- 过长号码
- 过长 ID
- 下划线账号等不适合朗读的内容

关闭后就完全保留原样。

---

## 七、推荐起步配置

### 方案 A：可爱活泼猫娘

- `voice = default_zh`
- `global_style = 俏皮,夹子音,开心`
- `speed_style = 稍快`
- `auto_tag_enabled = 开`
- `auto_tag_mode = llm`
- `auto_tag_density = aggressive`
- `tagger_strict_guidance_enabled = 开`
- `tagger_model = gemini-3-flash-preview`
- `auto_tag_profile = catgirl_soft`

### 方案 B：更元气

- `voice = default_zh`
- `global_style = 俏皮,夹子音,开心`
- `speed_style = 变快`
- `auto_tag_density = aggressive`
- `auto_tag_profile = catgirl_energetic`

### 方案 C：更温柔

- `voice = default_zh`
- `global_style = 温柔`
- `speed_style = 稍慢`
- `auto_tag_density = balanced`
- `auto_tag_profile = gentle`

---

## 八、调试建议

### 1. 看最终 assistant 文本

```text
/mimo_tts_preview_text 你好，这是一个测试
```

### 2. 只看自动打标结果

```text
/mimo_tts_preview_tagged 你好，这是一个测试
```

### 3. 打开 warning 日志开关

- `log_llm_tagged_text_warning`
- `log_tts_send_success_warning`

这样你能直接看到：

- LLM 最终打了什么标签
- 插件侧生成 wav 是否成功

---

## 九、关于“为什么有些标签会被直接念出来”

根本原因通常不是你配错了，而是：

- `<style>` 属于更稳定的大风格控制
- 括号标签属于弱控制，MiMo 有时会把它们当正文读出来

所以这版开始，插件默认更倾向：

- 大风格进 `<style>`
- 正文标签更像官方示例里的动作/状态/节奏控制
