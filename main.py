import base64
import os
import re
import uuid
from typing import List

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
import astrbot.api.message_components as Comp


class MimoTTSDecorator(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.temp_dir = self.config.get("temp_dir", "/AstrBot/data/temp/mimo_tts")
        os.makedirs(self.temp_dir, exist_ok=True)
        logger.info("[mimo_tts_decorator] loaded v0.5.0")

    def _cfg(self, key: str, default=None):
        return self.config.get(key, default)

    def _log_llm_tagged_text(self, tagged_text: str):
        if not self._cfg("log_llm_tagged_text_warning", False):
            return
        safe = (tagged_text or "").replace("\n", "\\n")
        if len(safe) > 500:
            safe = safe[:500] + "..."
        logger.warning(f"[mimo_tts_decorator] llm tagged text: {safe}")

    def _log_tts_send_success(self, source: str, wav_path: str, text: str = ""):
        if not self._cfg("log_tts_send_success_warning", False):
            return
        msg = f"[mimo_tts_decorator] tts success source={source} wav={wav_path}"
        if text:
            preview = text.replace("\n", "\\n")
            if len(preview) > 120:
                preview = preview[:120] + "..."
            msg += f" text={preview}"
        logger.warning(msg)

    def _extract_plain_text(self, chain) -> str:
        texts = []
        for comp in chain:
            if isinstance(comp, Comp.Plain):
                txt = getattr(comp, "text", "") or ""
                if txt:
                    texts.append(txt)
        return "".join(texts).strip()

    def _is_supported_chain(self, chain) -> bool:
        if not chain:
            return False

        for comp in chain:
            if isinstance(comp, Comp.Record):
                return False

        if not self._cfg("only_plain_chain", True):
            return True

        allowed = (Comp.Plain, Comp.At, Comp.Reply)
        return all(isinstance(comp, allowed) for comp in chain)

    def _normalize_text(self, text: str) -> str:
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[\t\f\v]+", " ", text)
        text = re.sub(r"[ ]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _sanitize_for_speech(self, text: str) -> str:
        """轻量清洗：去掉不适合朗读的 @ / 长号码 / 过长 ID 等。"""
        if not self._cfg("speech_cleanup_enabled", True):
            return text

        s = self._normalize_text(text)
        if not s:
            return s

        s = s.replace("@全体成员", "大家").replace("@全体", "大家")
        s = re.sub(r"@[^\s]+", "", s)
        s = s.replace("_", "")
        s = re.sub(r"\b\d{6,}\b", "某个号码", s)

        def _mask_token(m):
            tok = m.group(0)
            if len(tok) >= 10 and any(ch.isdigit() for ch in tok) and any(ch.isalpha() for ch in tok):
                return "某位玩家"
            return tok

        s = re.sub(r"\b[A-Za-z0-9]{10,}\b", _mask_token, s)
        s = re.sub(r"\s{2,}", " ", s).strip()
        return s

    def _looks_tagged(self, text: str) -> bool:
        s = (text or "").strip()
        if not s:
            return False
        head = s[:32]
        return head.startswith("<style>") or head.startswith("（") or head.startswith("(")

    def _split_sentences(self, text: str) -> List[str]:
        text = self._normalize_text(text)
        text = self._sanitize_for_speech(text)
        if not text:
            return []

        parts = re.split(r'([。！？!?；;…]+|\n+)', text)
        out = []
        buf = ""
        for part in parts:
            if not part:
                continue
            if re.fullmatch(r'([。！？!?；;…]+|\n+)', part):
                buf += part
                if buf.strip():
                    out.append(buf.strip())
                buf = ""
            else:
                if buf:
                    out.append(buf.strip())
                    buf = ""
                buf = part
        if buf.strip():
            out.append(buf.strip())
        return [x for x in out if x and x.strip()]

    def _dedupe_tags(self, tags: List[str]) -> List[str]:
        seen = set()
        out = []
        for tag in tags:
            tag = (tag or "").strip()
            if not tag or tag in seen:
                continue
            seen.add(tag)
            out.append(tag)
        return out

    def _tag(self, key: str, default: str) -> str:
        return (self._cfg(key, default) or "").strip()

    def _contains_any(self, text: str, words: List[str]) -> bool:
        return any(w in text for w in words)

    def _infer_rule_tags(self, sentence: str, idx: int) -> List[str]:
        s = (sentence or "").strip()
        if not s:
            return []

        tags: List[str] = []
        pause_tag = self._tag("auto_tag_pause_between_sentences", "（停顿）")
        think_tag = self._tag("auto_tag_think_tag", "（沉默片刻）")
        quiet_tag = self._tag("auto_tag_quiet_tag", "（小声）")
        speedup_tag = self._tag("auto_tag_speedup_tag", "（语速加快）")
        sigh_tag = self._tag("auto_tag_sigh_tag", "（长叹一口气）")
        smile_tag = self._tag("auto_tag_wry_smile_tag", "（苦笑）")
        cough_tag = self._tag("auto_tag_cough_tag", "（咳嗽）")
        loud_tag = self._tag("auto_tag_loud_tag", "（提高音量喊话）")
        breath_tag = self._tag("auto_tag_deep_breath_tag", "（紧张，深呼吸）")

        if idx > 0 and pause_tag:
            tags.append(pause_tag)

        # 更像官方示例的细粒度标签：动作 / 状态 / 节奏，而不是抽象风格词。
        if self._contains_any(s, ["唉", "哎", "唉呀", "叹", "没办法", "算了"]):
            tags.append(sigh_tag)
        if self._contains_any(s, ["咳", "咳咳"]):
            tags.append(cough_tag)
        if self._contains_any(s, ["紧张", "冷静", "稳住", "别慌", "深呼吸"]):
            tags.append(breath_tag)
        if self._contains_any(s, ["呵", "苦笑", "自嘲", "无奈"]):
            tags.append(smile_tag)
        if self._contains_any(s, ["等等", "等一下", "稍等", "先别急", "那个", "嗯", "呃"]):
            tags.append(think_tag)

        is_question = bool(re.search(r'[?？]+\s*$', s))
        is_exclaim = bool(re.search(r'[!！]+\s*$', s))
        has_urgent_words = self._contains_any(s, ["快", "赶紧", "马上", "快点", "来不及", "冲", "立刻"])
        has_loud_words = self._contains_any(s, ["注意", "大家", "住手", "别动", "快跑", "快走", "喂", "老板"])
        has_soft_words = self._contains_any(s, ["悄悄", "轻轻", "安静", "别怕", "慢慢来", "我在呢", "先别紧张"])

        profile = (self._cfg("auto_tag_profile", "catgirl_soft") or "catgirl_soft").strip()
        if profile == "catgirl_soft":
            if has_soft_words:
                tags.append(quiet_tag)
            if is_question and think_tag:
                tags.append(think_tag)
        elif profile == "catgirl_energetic":
            if has_urgent_words or is_exclaim:
                tags.append(speedup_tag)
            if has_loud_words and loud_tag:
                tags.append(loud_tag)
        elif profile == "gentle":
            if has_soft_words:
                tags.append(quiet_tag)
            if is_question and think_tag:
                tags.append(think_tag)
            if self._contains_any(s, ["辛苦", "抱抱", "没事", "别难过"]):
                tags.append(sigh_tag)
        elif profile == "neutral":
            if has_urgent_words and speedup_tag:
                tags.append(speedup_tag)

        # 少量通用规则
        if is_exclaim and has_loud_words:
            tags.append(loud_tag)
        elif is_exclaim and has_urgent_words:
            tags.append(speedup_tag)

        tags = self._dedupe_tags(tags)
        # 单句最多 2 个标签，避免堆叠
        return tags[:2]

    def _cleanup_generated_tag_text(self, text: str) -> str:
        """对自动打标生成的文本做后处理：
        - 把 ASCII () 标签规范成中文（）
        - 避免插件自身常见的抽象标签进入正文
        - 把 （轻声） 归一成更接近官方示例的 （小声）
        - 控制开头标签数量
        """
        s = self._normalize_text(text)
        if not s:
            return s

        # 规范短标签括号
        s = re.sub(r'\(([^()\n]{1,18})\)', lambda m: f"（{m.group(1).strip()}）", s)

        replacements = {
            "（轻声）": "（小声）",
            "（低声）": "（小声）",
        }
        for old, new in replacements.items():
            s = s.replace(old, new)

        # 对自动生成结果做收敛：抽象风格词不要作为正文标签直接念出来。
        risky_labels = [
            "轻快", "轻盈灵动", "带一点撒娇", "元气一点", "句尾上扬",
            "活泼一点", "温柔一点", "可爱", "俏皮", "撒娇", "软萌",
        ]
        for label in risky_labels:
            s = re.sub(rf'（\s*{re.escape(label)}\s*）', '', s)

        s = re.sub(r'（\s*）', '', s)
        s = re.sub(r'(）)\s+(（)', r'\1\2', s)
        s = re.sub(r'\s{2,}', ' ', s).strip()

        # 开头最多保留 2 个标签
        m = re.match(r'^((?:（[^（）\n]{1,24}）)+)(.*)$', s)
        if m:
            tags = re.findall(r'（[^（）\n]{1,24}）', m.group(1))
            tags = self._dedupe_tags(tags)[:2]
            s = ''.join(tags) + m.group(2)
        return s.strip()

    def _auto_tag_rule_based(self, text: str) -> str:
        text = self._normalize_text(text)
        text = self._sanitize_for_speech(text)
        if not text:
            return ""

        if self._cfg("auto_tag_skip_if_already_tagged", True) and self._looks_tagged(text):
            return text

        sentences = self._split_sentences(text)
        if not sentences:
            return text

        out = []
        for idx, sentence in enumerate(sentences):
            tags = self._infer_rule_tags(sentence, idx)
            out.append("".join(tags) + sentence)
        return self._cleanup_generated_tag_text("".join(out).strip())

    def _strict_guidance_appendix(self) -> str:
        return (
            "\n\n额外硬性要求："
            "\n1. 只输出最终可朗读文本，不要前言、不要解释、不要总结、不要说测试感想。"
            "\n2. 允许轻度口语化和断句优化，但不要自由扩写，不要凭空新增内容。"
            "\n3. 若需要括号标签，请尽量模仿 MiMo 官方细粒度示例，优先使用动作/状态/节奏类标签，例如："
            "（停顿）（小声）（沉默片刻）（长叹一口气）（语速加快）（苦笑）（咳嗽）（提高音量喊话）（紧张，深呼吸）。"
            "\n4. 不要输出抽象风格标签作为正文内容，例如："
            "（轻快）（轻盈灵动）（带一点撒娇）（元气一点）（句尾上扬）等。"
            "\n5. 每段最多插入 0 到 2 处标签，优先放在句间或局部位置，不要在开头堆叠很多标签。"
            "\n6. 删除或改写不适合朗读的内容：@提及、QQ号/UID 等长数字、过长 ID、下划线账号。"
        )

    def _default_tagger_prompt(self) -> str:
        return (
            "你是一个中文 TTS 朗读润色器。"
            "你的任务是：把用户原文改写成更适合 MiMo TTS 朗读的中文口语短句，"
            "必要时加入少量更像官方示例的细粒度音频标签。"
            "要求：保持原意，适度口语化、断句、压缩长句，让文本更适合语音聊天。"
        )

    def _compose_tagger_system_prompt(self) -> str:
        custom_prompt = (self._cfg("tagger_system_prompt", "") or "").strip()
        strict_enabled = bool(self._cfg("tagger_strict_guidance_enabled", True))
        base_prompt = custom_prompt or self._default_tagger_prompt()
        if strict_enabled:
            base_prompt += self._strict_guidance_appendix()
        return base_prompt.strip()

    async def _auto_tag_llm(self, text: str) -> str:
        api_key = (self._cfg("tagger_api_key", "") or "").strip()
        base_url = (self._cfg("tagger_base_url", "") or "").strip()
        model = (self._cfg("tagger_model", "") or "").strip()
        if not api_key or not base_url or not model:
            raise RuntimeError("自动标签 LLM 模式已开启，但 tagger_api_key / tagger_base_url / tagger_model 未配置完整")

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": self._compose_tagger_system_prompt()},
                {"role": "user", "content": text},
            ],
            "temperature": float(self._cfg("tagger_temperature", 0.3)),
        }
        timeout = int(self._cfg("tagger_timeout_seconds", 45))
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(base_url, headers=headers, json=payload)

        if resp.status_code >= 400:
            raise RuntimeError(f"Tagger HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception as e:
            raise RuntimeError(f"Tagger 响应缺少 message.content: {data}") from e

        content = self._cleanup_generated_tag_text(content)
        if not content:
            raise RuntimeError("Tagger 返回了空文本")
        self._log_llm_tagged_text(content)
        return content

    async def _maybe_auto_tag(self, text: str) -> str:
        text = self._normalize_text(text)
        text = self._sanitize_for_speech(text)
        if not text:
            return ""
        if not self._cfg("auto_tag_enabled", False):
            return text
        if len(text) > int(self._cfg("auto_tag_max_chars", 280)):
            return text

        mode = (self._cfg("auto_tag_mode", "rule_based") or "rule_based").strip()
        if mode == "off":
            return text
        if mode == "rule_based":
            return self._auto_tag_rule_based(text)
        if mode == "llm":
            try:
                return await self._auto_tag_llm(text)
            except Exception as e:
                if self._cfg("auto_tag_llm_fallback_to_rule", True):
                    logger.warning(f"[mimo_tts_decorator] auto tag llm failed, fallback to rule_based: {e}")
                    return self._auto_tag_rule_based(text)
                raise
        return text

    def _build_style_value(self) -> str:
        parts = []
        global_style = (self._cfg("global_style", "") or "").strip()
        speed_style = (self._cfg("speed_style", "") or "").strip()

        if global_style:
            for part in re.split(r"[，,]+", global_style):
                part = (part or "").strip()
                if part:
                    parts.append(part)

        if speed_style:
            parts.append(speed_style)

        deduped = []
        seen = set()
        for part in parts:
            if part in seen:
                continue
            seen.add(part)
            deduped.append(part)
        return ",".join(deduped)

    def _build_assistant_text(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""

        style_value = self._build_style_value()
        audio_tag_prefix = (self._cfg("audio_tag_prefix", "") or "")
        audio_tag_suffix = (self._cfg("audio_tag_suffix", "") or "")
        style_tag = ""

        if style_value and not text.lstrip().startswith("<style>"):
            style_tag = f"<style>{style_value}</style>"

        template = self._cfg(
            "assistant_text_template",
            "{style_tag}{audio_tag_prefix}{text}{audio_tag_suffix}",
        )
        try:
            built = template.format(
                style_tag=style_tag,
                style_value=style_value,
                audio_tag_prefix=audio_tag_prefix,
                text=text,
                audio_tag_suffix=audio_tag_suffix,
            )
        except Exception:
            built = f"{style_tag}{audio_tag_prefix}{text}{audio_tag_suffix}"

        return built.strip()

    async def _prepare_assistant_text(self, text: str) -> str:
        tagged = await self._maybe_auto_tag(text)
        built = self._build_assistant_text(tagged)
        return built

    async def _call_mimo_tts(self, text: str) -> str:
        api_key = (self._cfg("api_key", "") or "").strip()
        if not api_key:
            raise RuntimeError("MiMo API Key 未配置")

        base_url = (self._cfg("base_url", "") or "").strip()
        if not base_url:
            raise RuntimeError("MiMo base_url 未配置")

        assistant_text = await self._prepare_assistant_text(text)
        if not assistant_text:
            raise RuntimeError("待合成文本为空")

        payload = {
            "model": self._cfg("model", "mimo-v2-tts"),
            "messages": [
                {
                    "role": "user",
                    "content": self._cfg(
                        "dummy_user_prompt",
                        "请把 assistant 提供的文本转成自然、清晰、稳定的普通话语音，不要改写 assistant 文本。",
                    ),
                },
                {
                    "role": "assistant",
                    "content": assistant_text,
                },
            ],
            "audio": {
                "format": self._cfg("format", "wav"),
                "voice": self._cfg("voice", "default_zh"),
            },
            "temperature": 0,
        }

        timeout = int(self._cfg("timeout_seconds", 60))
        headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(base_url, headers=headers, json=payload)

        content_type = resp.headers.get("content-type", "")
        if resp.status_code >= 400:
            raise RuntimeError(
                f"MiMo HTTP {resp.status_code}, content-type={content_type}, body={resp.text[:300]}"
            )

        if "html" in content_type.lower():
            raise RuntimeError(f"MiMo 返回了 HTML 而不是 JSON：{resp.text[:300]}")

        data = resp.json()

        try:
            audio_b64 = data["choices"][0]["message"]["audio"]["data"]
        except Exception as e:
            raise RuntimeError(f"MiMo 响应里没有 audio.data：{data}") from e

        raw = base64.b64decode(audio_b64)
        if len(raw) < 64:
            raise RuntimeError("MiMo 返回的音频过小，疑似无效响应")

        if not (raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"):
            raise RuntimeError("MiMo 返回内容不是标准 WAV（缺少 RIFF/WAVE 头）")

        path = os.path.join(self.temp_dir, f"mimo_tts_{uuid.uuid4().hex}.wav")
        with open(path, "wb") as f:
            f.write(raw)
        return path

    @filter.command("mimo_tts_preview_text")
    async def mimo_tts_preview_text(self, event: AstrMessageEvent, text: str):
        if not text or not text.strip():
            yield event.plain_result("用法：/mimo_tts_preview_text 你好，这是一个测试")
            return
        try:
            built = await self._prepare_assistant_text(text.strip())
            yield event.plain_result(built)
        except Exception as e:
            yield event.plain_result(f"预览失败：{e}")

    @filter.command("mimo_tts_preview_tagged")
    async def mimo_tts_preview_tagged(self, event: AstrMessageEvent, text: str):
        if not text or not text.strip():
            yield event.plain_result("用法：/mimo_tts_preview_tagged 你好，这是一个测试")
            return
        try:
            tagged = await self._maybe_auto_tag(text.strip())
            yield event.plain_result(tagged)
        except Exception as e:
            yield event.plain_result(f"标签预览失败：{e}")

    @filter.command("mimo_tts_test")
    async def mimo_tts_test(self, event: AstrMessageEvent, text: str):
        if not text or not text.strip():
            yield event.plain_result("用法：/mimo_tts_test 你好，这是一个测试")
            return

        try:
            clean_text = text.strip()
            wav_path = await self._call_mimo_tts(clean_text)
            self._log_tts_send_success("mimo_tts_test", wav_path, clean_text)
            yield event.chain_result([Comp.Record(file=wav_path, url=wav_path)])
        except Exception as e:
            logger.exception("[mimo_tts_decorator] mimo_tts_test failed")
            yield event.plain_result(f"MiMo TTS 失败：{e}")

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        if not self._cfg("enabled", True):
            return
        if not self._cfg("decorator_enabled", True):
            return

        result = event.get_result()
        if not result or not getattr(result, "chain", None):
            return

        chain = result.chain
        if not self._is_supported_chain(chain):
            return

        text = self._extract_plain_text(chain)
        if not text:
            return

        max_chars = int(self._cfg("max_chars", 500))
        if len(text) > max_chars:
            logger.warning(f"[mimo_tts_decorator] 文本过长，跳过 TTS: {len(text)} > {max_chars}")
            return

        try:
            wav_path = await self._call_mimo_tts(text)
        except Exception as e:
            logger.exception(f"[mimo_tts_decorator] TTS failed, keep original text. err={e}")
            return

        mode = self._cfg("trigger_mode", "replace_plain")
        if mode == "append_record":
            if self._cfg("preserve_text_when_append", True):
                chain.append(Comp.Record(file=wav_path, url=wav_path))
            else:
                result.chain = [Comp.Record(file=wav_path, url=wav_path)]
        else:
            result.chain = [Comp.Record(file=wav_path, url=wav_path)]

        self._log_tts_send_success("decorator", wav_path, text)

    async def terminate(self):
        logger.info("[mimo_tts_decorator] terminate")
