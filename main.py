import asyncio
import base64
import binascii
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, List, Optional

import astrbot.api.message_components as Comp
import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

PLUGIN_NAME = "astrbot_plugin_mimo_tts_decorator"
LEGACY_TEMP_DIR = "/AstrBot/data/temp/mimo_tts"
EVENT_TEMP_FILES_ATTR = "_mimo_tts_temp_files"
TAGGER_BASE_URL_PLACEHOLDER = "http://xxx.xxx.xxx/v1/chat/completions"
MIMO_DEFAULT_CHAT_COMPLETIONS_URL = "https://api.xiaomimimo.com/v1/chat/completions"
RE_TABLIKE_WHITESPACE = re.compile(r"[\t\f\v]+")
RE_MULTI_SPACES = re.compile(r"[ ]{2,}")
RE_MULTI_NEWLINES = re.compile(r"\n{3,}")
RE_AT_MENTION = re.compile(
    r"@[^\s@，。！？!,,:：;；、]{1,24}(?=$|[\s，。！？!,,:：;；、])"
)
RE_LONG_NUMBER = re.compile(r"\b\d{6,}\b")
RE_LONG_MIXED_TOKEN = re.compile(r"\b[A-Za-z0-9]{10,}\b")
RE_SENTENCE_SPLIT = re.compile(r"([。！？!?；;…]+|\n+)")
RE_QUESTION_END = re.compile(r"[?？]+\s*$")
RE_EXCLAIM_END = re.compile(r"[!！]+\s*$")
RE_ASCII_TAG = re.compile(r"\(([^()\n]{1,18})\)")
RE_EMPTY_CN_TAG = re.compile(r"（\s*）")
RE_ADJACENT_TAG_SPACE = re.compile(r"(）)\s+(（)")
RE_LEADING_TAGS = re.compile(r"^((?:（[^（）\n]{1,24}）)+)(.*)$")
RE_LEADING_TAG = re.compile(r"（[^（）\n]{1,24}）")


class MimoTTSDecorator(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._http_client: Optional[httpx.AsyncClient] = None
        self.temp_dir = self._resolve_temp_dir()
        os.makedirs(self.temp_dir, exist_ok=True)
        self._cleanup_stale_temp_files()
        logger.info("[mimo_tts_decorator] loaded v1.0.0")

    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def _plugin_name(self) -> str:
        return getattr(self, "name", "") or PLUGIN_NAME

    def _get_tts_provider_id(self) -> str:
        return (self._cfg("tts_provider_id", "") or "").strip()

    def _default_temp_dir(self) -> str:
        try:
            data_path = Path(StarTools.get_data_dir())
            return str(data_path / "temp")
        except Exception as e:
            logger.warning(
                "[mimo_tts_decorator] resolve AstrBot data dir failed, "
                f"fallback to cwd: {e}"
            )
        return str(Path.cwd() / "data" / "plugin_data" / self._plugin_name() / "temp")

    def _match_provider_id(self, provider: Any, provider_id: str) -> bool:
        provider_id = (provider_id or "").strip()
        if not provider_id or provider is None:
            return False

        candidate_values = [
            getattr(provider, "id", None),
            getattr(provider, "provider_id", None),
        ]
        provider_config = getattr(provider, "provider_config", None)
        if isinstance(provider_config, dict):
            candidate_values.extend(
                [
                    provider_config.get("id"),
                    provider_config.get("provider_id"),
                    provider_config.get("name"),
                ]
            )

        return any((value or "").strip() == provider_id for value in candidate_values)

    def _get_selected_tts_provider(self) -> Any:
        provider_id = self._get_tts_provider_id()
        if not provider_id:
            return None

        try:
            providers = self.context.get_all_tts_providers()
        except Exception as e:
            raise RuntimeError(f"获取 AstrBot TTS 提供商列表失败：{e}") from e

        for provider in providers or []:
            if self._match_provider_id(provider, provider_id):
                return provider

        raise RuntimeError(f"TTS 提供商不存在或不可用：{provider_id}")

    def _get_provider_config(self, provider: Any) -> dict:
        provider_config = getattr(provider, "provider_config", None)
        if isinstance(provider_config, dict):
            return provider_config
        return {}

    def _is_selected_provider_mimo_tts(self, provider: Any) -> bool:
        provider_config = self._get_provider_config(provider)
        provider_type = (provider_config.get("type", "") or "").strip()
        if provider_type == "mimo_tts_api":
            return True

        provider_id = self._get_tts_provider_id()
        if provider_id.startswith("mimo"):
            return True

        provider_name = (provider_config.get("name", "") or "").lower()
        return "mimo" in provider_name

    def _normalize_mimo_base_url(self, base_url: str) -> str:
        normalized = (base_url or "").strip().rstrip("/")
        if not normalized:
            return normalized
        if normalized.endswith("/chat/completions"):
            return normalized
        return normalized + "/chat/completions"

    def _resolve_mimo_base_url_from_provider_config(self, provider_config: dict) -> str:
        candidates = [
            provider_config.get("base_url"),
            provider_config.get("api_base"),
            provider_config.get("api_url"),
            provider_config.get("endpoint"),
            provider_config.get("url"),
        ]
        for candidate in candidates:
            normalized = self._normalize_mimo_base_url(candidate or "")
            if not normalized:
                continue
            lowered = normalized.lower()
            if "platform.xiaomimimo.com" in lowered:
                logger.warning(
                    "[mimo_tts_decorator] ignore MiMo provider web console url: "
                    f"{normalized}"
                )
                continue
            return normalized
        logger.info(
            "[mimo_tts_decorator] "
            "use default MiMo API endpoint from provider fallback: "
            f"{MIMO_DEFAULT_CHAT_COMPLETIONS_URL}"
        )
        return MIMO_DEFAULT_CHAT_COMPLETIONS_URL

    def _resolve_mimo_request_settings_from_provider(self, provider: Any) -> dict:
        provider_config = self._get_provider_config(provider)
        api_key = (provider_config.get("api_key", "") or "").strip()
        base_url = self._resolve_mimo_base_url_from_provider_config(provider_config)
        model = (provider_config.get("model", "") or "").strip()
        voice = (provider_config.get("mimo-tts-voice", "") or "").strip()
        audio_format = (provider_config.get("mimo-tts-format", "") or "").strip()

        if not api_key:
            raise RuntimeError("官方 MiMo TTS 提供商里没有可复用的 API Key")
        if not base_url:
            raise RuntimeError("官方 MiMo TTS 提供商里没有可复用的 API 地址")

        return {
            "api_key": api_key,
            "base_url": base_url,
            "model": model or self._cfg("model", "mimo-v2-tts"),
            "voice": voice or self._cfg("voice", "default_zh"),
            "format": audio_format or self._cfg("format", "wav"),
        }

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(follow_redirects=True)
        return self._http_client

    async def _post_json(
        self,
        api_name: str,
        url: str,
        headers: dict,
        payload: dict,
        timeout_seconds: int,
    ) -> dict:
        client = self._get_http_client()
        try:
            resp = await client.post(
                url, headers=headers, json=payload, timeout=timeout_seconds
            )
        except httpx.HTTPError as e:
            raise RuntimeError(f"{api_name} 请求失败：{e}") from e

        content_type = resp.headers.get("content-type", "")
        body_preview = resp.text[:300]
        if resp.status_code >= 400:
            raise RuntimeError(
                f"{api_name} HTTP {resp.status_code}, "
                f"content-type={content_type}, body={body_preview}"
            )
        if "html" in content_type.lower():
            raise RuntimeError(f"{api_name} 返回了 HTML 而不是 JSON：{body_preview}")
        try:
            return resp.json()
        except ValueError as e:
            raise RuntimeError(
                f"{api_name} 返回的不是合法 JSON, "
                f"content-type={content_type}, body={body_preview}"
            ) from e

    def _resolve_temp_dir(self) -> str:
        configured = (self.config.get("temp_dir", "") or "").strip()
        if not configured or configured == LEGACY_TEMP_DIR:
            return self._default_temp_dir()
        return configured

    def _cleanup_stale_temp_files(self):
        retention_hours = int(self._cfg("temp_file_retention_hours", 24))
        if retention_hours <= 0:
            return

        expire_before = time.time() - retention_hours * 3600
        removed = 0
        for entry in os.scandir(self.temp_dir):
            if not entry.is_file():
                continue
            if not entry.name.startswith("mimo_tts_") or not entry.name.endswith(
                ".wav"
            ):
                continue
            try:
                if entry.stat().st_mtime > expire_before:
                    continue
                os.remove(entry.path)
                removed += 1
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning(
                    "[mimo_tts_decorator] cleanup stale temp file failed: "
                    f"{entry.path}, err={e}"
                )
        if removed:
            logger.info(f"[mimo_tts_decorator] cleaned {removed} stale temp wav files")

    def _track_temp_file(self, event: AstrMessageEvent, wav_path: str):
        tracked = getattr(event, EVENT_TEMP_FILES_ATTR, None)
        if tracked is None:
            tracked = []
            setattr(event, EVENT_TEMP_FILES_ATTR, tracked)
        if wav_path not in tracked:
            tracked.append(wav_path)

    def _make_record(self, event: AstrMessageEvent, wav_path: str) -> Comp.Record:
        self._track_temp_file(event, wav_path)
        return Comp.Record(file=wav_path, url=wav_path)

    def _replace_plain_with_record(self, chain, record: Comp.Record):
        new_chain = []
        inserted = False
        for comp in chain:
            if isinstance(comp, Comp.Plain):
                if not inserted:
                    new_chain.append(record)
                    inserted = True
                continue
            new_chain.append(comp)
        if not inserted:
            new_chain.append(record)
        return new_chain

    def _cleanup_tracked_event_files(self, event: AstrMessageEvent):
        tracked = list(dict.fromkeys(getattr(event, EVENT_TEMP_FILES_ATTR, []) or []))
        if not tracked:
            return

        for path in tracked:
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning(
                    f"[mimo_tts_decorator] remove temp wav failed: {path}, err={e}"
                )
        setattr(event, EVENT_TEMP_FILES_ATTR, [])

    def _is_local_file_path(self, path: str) -> bool:
        if not path or "://" in path:
            return False
        return os.path.isfile(path)

    def _copy_audio_into_temp_dir(self, source_path: str) -> str:
        if not self._is_local_file_path(source_path):
            return source_path

        source = Path(source_path)
        try:
            if source.resolve().parent == Path(self.temp_dir).resolve():
                return str(source)
        except Exception:
            pass

        suffix = source.suffix or ".wav"
        target = Path(self.temp_dir) / f"mimo_tts_{uuid.uuid4().hex}{suffix}"
        shutil.copyfile(source, target)
        return str(target)

    def _looks_like_wav_file(self, path: str) -> bool:
        if not self._is_local_file_path(path):
            return False
        try:
            with open(path, "rb") as f:
                head = f.read(12)
            return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"
        except Exception:
            return False

    def _normalize_audio_for_qq(self, source_path: str) -> str:
        if not self._is_local_file_path(source_path):
            return source_path

        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return source_path

        target = Path(self.temp_dir) / f"mimo_tts_{uuid.uuid4().hex}_qq.wav"
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            source_path,
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ac",
            "1",
            "-ar",
            "24000",
            str(target),
        ]
        try:
            completed = subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as e:
            logger.warning(
                "[mimo_tts_decorator] ffmpeg normalize failed to start: "
                f"{source_path}, err={e}"
            )
            return source_path

        if completed.returncode != 0 or not target.is_file():
            logger.warning(
                "[mimo_tts_decorator] ffmpeg normalize failed: "
                f"source={source_path}, code={completed.returncode}, "
                f"stderr={completed.stderr[-300:]}"
            )
            return source_path
        return str(target)

    async def _call_mimo_http(
        self,
        assistant_text: str,
        *,
        api_key: str,
        base_url: str,
        model: str,
        voice: str,
        audio_format: str,
    ) -> str:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": self._cfg(
                        "dummy_user_prompt",
                        "请把 assistant 提供的文本转成自然、清晰、"
                        "稳定的普通话语音，"
                        "不要改写 assistant 文本。",
                    ),
                },
                {
                    "role": "assistant",
                    "content": assistant_text,
                },
            ],
            "audio": {
                "format": audio_format,
                "voice": voice,
            },
            "temperature": 0,
        }

        timeout = int(self._cfg("timeout_seconds", 60))
        headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
        }
        data = await self._post_json("MiMo", base_url, headers, payload, timeout)

        try:
            audio_b64 = data["choices"][0]["message"]["audio"]["data"]
        except Exception as e:
            raise RuntimeError(f"MiMo 响应里没有 audio.data：{data}") from e

        try:
            raw = base64.b64decode(audio_b64, validate=True)
        except (binascii.Error, TypeError, ValueError) as e:
            raise RuntimeError("MiMo 返回的 audio.data 不是合法 Base64") from e
        if len(raw) < 64:
            raise RuntimeError("MiMo 返回的音频过小，疑似无效响应")

        if not (raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"):
            raise RuntimeError("MiMo 返回内容不是标准 WAV（缺少 RIFF/WAVE 头）")

        path = str(
            Path(self.temp_dir) / f"mimo_tts_{uuid.uuid4().hex}.{audio_format or 'wav'}"
        )
        with open(path, "wb") as f:
            f.write(raw)
        return path

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
        text = RE_TABLIKE_WHITESPACE.sub(" ", text)
        text = RE_MULTI_SPACES.sub(" ", text)
        text = RE_MULTI_NEWLINES.sub("\n\n", text)
        return text.strip()

    def _sanitize_for_speech(self, text: str) -> str:
        """轻量清洗：去掉不适合朗读的 @ / 长号码 / 过长 ID 等。"""
        if not self._cfg("speech_cleanup_enabled", True):
            return text

        s = self._normalize_text(text)
        if not s:
            return s

        s = s.replace("@全体成员", "大家").replace("@全体", "大家")
        s = RE_AT_MENTION.sub("", s)
        s = s.replace("_", "")
        s = RE_LONG_NUMBER.sub("某个号码", s)

        def _mask_token(m):
            tok = m.group(0)
            if (
                len(tok) >= 10
                and any(ch.isdigit() for ch in tok)
                and any(ch.isalpha() for ch in tok)
            ):
                return "某位玩家"
            return tok

        s = RE_LONG_MIXED_TOKEN.sub(_mask_token, s)
        s = RE_MULTI_SPACES.sub(" ", s).strip()
        return s

    def _looks_tagged(self, text: str) -> bool:
        s = (text or "").strip()
        if not s:
            return False
        head = s[:32]
        return (
            head.startswith("<style>") or head.startswith("（") or head.startswith("(")
        )

    def _split_sentences(self, text: str) -> List[str]:
        text = self._normalize_text(text)
        text = self._sanitize_for_speech(text)
        if not text:
            return []

        parts = RE_SENTENCE_SPLIT.split(text)
        out = []
        buf = ""
        for part in parts:
            if not part:
                continue
            if RE_SENTENCE_SPLIT.fullmatch(part):
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

    def _auto_tag_density(self) -> str:
        density = (self._cfg("auto_tag_density", "balanced") or "balanced").strip()
        if density not in {"conservative", "balanced", "aggressive"}:
            return "balanced"
        return density

    def _max_tags_per_sentence(self) -> int:
        density = self._auto_tag_density()
        if density == "conservative":
            return 2
        if density == "aggressive":
            return 4
        return 3

    def _llm_density_guidance(self) -> str:
        density = self._auto_tag_density()
        if density == "conservative":
            return (
                "按句意谨慎插入标签，整段通常 1 到 3 处即可。"
                "优先在句间停顿、迟疑、强调位置添加，避免标签太多。"
            )
        if density == "aggressive":
            return (
                "按句意更积极地插入标签，整段通常 4 到 8 处，长段落可更多。"
                "优先在句间停顿、情绪转折、强调、呼吸感位置分散添加，"
                "但不要连续堆叠 3 个以上标签。"
            )
        return (
            "按句意自然插入标签，整段通常 2 到 6 处。"
            "优先在句间停顿、情绪转折、强调位置分散添加，"
            "避免一整段只有 0 到 1 个标签。"
        )

    def _get_tagger_provider_id(self) -> str:
        return (self._cfg("tagger_provider_id", "") or "").strip()

    def _is_tagger_configured(self) -> bool:
        if self._get_tagger_provider_id():
            return True
        api_key = (self._cfg("tagger_api_key", "") or "").strip()
        base_url = (self._cfg("tagger_base_url", "") or "").strip()
        model = (self._cfg("tagger_model", "") or "").strip()
        if base_url == TAGGER_BASE_URL_PLACEHOLDER:
            return False
        return bool(api_key and base_url and model)

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
        if self._contains_any(
            s, ["等等", "等一下", "稍等", "先别急", "那个", "嗯", "呃"]
        ):
            tags.append(think_tag)

        is_question = bool(RE_QUESTION_END.search(s))
        is_exclaim = bool(RE_EXCLAIM_END.search(s))
        has_urgent_words = self._contains_any(
            s, ["快", "赶紧", "马上", "快点", "来不及", "冲", "立刻"]
        )
        has_loud_words = self._contains_any(
            s, ["注意", "大家", "住手", "别动", "快跑", "快走", "喂", "老板"]
        )
        has_soft_words = self._contains_any(
            s, ["悄悄", "轻轻", "安静", "别怕", "慢慢来", "我在呢", "先别紧张"]
        )

        profile = (
            self._cfg("auto_tag_profile", "catgirl_soft") or "catgirl_soft"
        ).strip()
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
        # 单句标签数量受密度配置控制，避免堆叠过多。
        return tags[: self._max_tags_per_sentence()]

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
        s = RE_ASCII_TAG.sub(lambda m: f"（{m.group(1).strip()}）", s)

        replacements = {
            "（轻声）": "（小声）",
            "（低声）": "（小声）",
        }
        for old, new in replacements.items():
            s = s.replace(old, new)

        # 对自动生成结果做收敛：抽象风格词不要作为正文标签直接念出来。
        risky_labels = [
            "轻快",
            "轻盈灵动",
            "带一点撒娇",
            "元气一点",
            "句尾上扬",
            "活泼一点",
            "温柔一点",
            "可爱",
            "俏皮",
            "撒娇",
            "软萌",
        ]
        for label in risky_labels:
            s = re.sub(rf"（\s*{re.escape(label)}\s*）", "", s)

        s = RE_EMPTY_CN_TAG.sub("", s)
        s = RE_ADJACENT_TAG_SPACE.sub(r"\1\2", s)
        s = RE_MULTI_SPACES.sub(" ", s).strip()

        # 开头最多保留 2 个标签
        m = RE_LEADING_TAGS.match(s)
        if m:
            tags = RE_LEADING_TAG.findall(m.group(1))
            tags = self._dedupe_tags(tags)[:2]
            s = "".join(tags) + m.group(2)
        return s.strip()

    def _auto_tag_rule_based(self, text: str) -> str:
        text = self._normalize_text(text)
        text = self._sanitize_for_speech(text)
        if not text:
            return ""

        if self._cfg("auto_tag_skip_if_already_tagged", True) and self._looks_tagged(
            text
        ):
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
            "\n3. 若需要括号标签，请尽量模仿 MiMo 官方细粒度示例，"
            "优先使用动作/状态/节奏类标签，例如："
            "（停顿）（小声）（沉默片刻）（长叹一口气）（语速加快）（苦笑）（咳嗽）（提高音量喊话）（紧张，深呼吸）。"
            "\n4. 不要输出抽象风格标签作为正文内容，例如："
            "（轻快）（轻盈灵动）（带一点撒娇）（元气一点）（句尾上扬）等。"
            "\n5. "
            + self._llm_density_guidance()
            + " 优先放在句间或局部位置，不要在开头堆叠很多标签。"
            "\n6. 删除或改写不适合朗读的内容："
            "@提及、QQ号/UID 等长数字、过长 ID、下划线账号。"
        )

    def _default_tagger_prompt(self) -> str:
        return (
            "你是一个中文 TTS 朗读润色器。"
            "你的任务是：把用户原文改写成更适合 MiMo TTS 朗读的中文口语短句，"
            "按句意和节奏自然加入适量、分散的细粒度音频标签。"
            "要求：保持原意，适度口语化、断句、压缩长句，让文本更适合语音聊天，"
            "并尽量接近 MiMo 官方示例那种按句分布的标签风格。"
        )

    def _compose_tagger_system_prompt(self) -> str:
        custom_prompt = (self._cfg("tagger_system_prompt", "") or "").strip()
        strict_enabled = bool(self._cfg("tagger_strict_guidance_enabled", True))
        base_prompt = custom_prompt or self._default_tagger_prompt()
        if strict_enabled:
            base_prompt += self._strict_guidance_appendix()
        return base_prompt.strip()

    async def _auto_tag_llm(self, text: str) -> str:
        provider_id = self._get_tagger_provider_id()
        if provider_id:
            timeout = int(self._cfg("tagger_timeout_seconds", 45))
            try:
                llm_resp = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=text,
                        system_prompt=self._compose_tagger_system_prompt(),
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError as e:
                raise RuntimeError(
                    "自动标签提供商调用超时"
                    f"(provider_id={provider_id}, timeout={timeout}s)"
                ) from e
            except Exception as e:
                raise RuntimeError(
                    f"自动标签提供商调用失败(provider_id={provider_id}): {e}"
                ) from e
            content = (getattr(llm_resp, "completion_text", "") or "").strip()
            if not content:
                raise RuntimeError(
                    f"自动标签提供商返回了空文本(provider_id={provider_id})"
                )
            content = self._cleanup_generated_tag_text(content)
            if not content:
                raise RuntimeError("Tagger 返回了空文本")
            self._log_llm_tagged_text(content)
            return content

        if not self._is_tagger_configured():
            raise RuntimeError(
                "自动标签 LLM 模式已开启，但 "
                "tagger_provider_id 或 "
                "tagger_api_key / tagger_base_url / "
                "tagger_model 未配置完整"
            )
        api_key = (self._cfg("tagger_api_key", "") or "").strip()
        base_url = (self._cfg("tagger_base_url", "") or "").strip()
        model = (self._cfg("tagger_model", "") or "").strip()

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

        data = await self._post_json("Tagger", base_url, headers, payload, timeout)
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
            if not self._is_tagger_configured():
                return self._auto_tag_rule_based(text)
            try:
                return await self._auto_tag_llm(text)
            except Exception as e:
                if self._cfg("auto_tag_llm_fallback_to_rule", True):
                    logger.warning(
                        "[mimo_tts_decorator] auto tag llm failed, "
                        f"fallback to rule_based: {e}"
                    )
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
        audio_tag_prefix = self._cfg("audio_tag_prefix", "") or ""
        audio_tag_suffix = self._cfg("audio_tag_suffix", "") or ""
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
        assistant_text = await self._prepare_assistant_text(text)
        if not assistant_text:
            raise RuntimeError("待合成文本为空")

        selected_tts_provider = self._get_selected_tts_provider()
        if (
            selected_tts_provider is not None
            and self._is_selected_provider_mimo_tts(selected_tts_provider)
        ):
            mimo_settings = self._resolve_mimo_request_settings_from_provider(
                selected_tts_provider
            )
            return await self._call_mimo_http(
                assistant_text,
                api_key=mimo_settings["api_key"],
                base_url=mimo_settings["base_url"],
                model=mimo_settings["model"],
                voice=mimo_settings["voice"],
                audio_format=mimo_settings["format"],
            )

        if selected_tts_provider is not None:
            timeout = int(self._cfg("timeout_seconds", 60))
            try:
                provider_audio = await asyncio.wait_for(
                    selected_tts_provider.get_audio(assistant_text),
                    timeout=timeout,
                )
                if not provider_audio:
                    raise RuntimeError("官方 TTS 提供商返回了空音频路径")
                copied_audio = self._copy_audio_into_temp_dir(provider_audio)
                normalized_audio = self._normalize_audio_for_qq(copied_audio)
                if normalized_audio != copied_audio and os.path.isfile(copied_audio):
                    try:
                        os.remove(copied_audio)
                    except OSError:
                        pass
                if not self._looks_like_wav_file(normalized_audio):
                    raise RuntimeError(
                        "官方 TTS 提供商返回的音频不是可识别的 WAV 文件，"
                        "当前无法发送为 QQ 语音"
                    )
                return normalized_audio
            except asyncio.TimeoutError as e:
                raise RuntimeError(
                    "官方 TTS 提供商调用超时"
                    f"(provider_id={self._get_tts_provider_id()}, timeout={timeout}s)"
                ) from e
            except Exception as e:
                raise RuntimeError(
                    "官方 TTS 提供商调用失败"
                    f"(provider_id={self._get_tts_provider_id()}): {e}"
                ) from e

        api_key = (self._cfg("api_key", "") or "").strip()
        if not api_key:
            raise RuntimeError("MiMo API Key 未配置")

        base_url = (self._cfg("base_url", "") or "").strip()
        if not base_url:
            raise RuntimeError("MiMo base_url 未配置")
        return await self._call_mimo_http(
            assistant_text,
            api_key=api_key,
            base_url=base_url,
            model=self._cfg("model", "mimo-v2-tts"),
            voice=self._cfg("voice", "default_zh"),
            audio_format=self._cfg("format", "wav"),
        )

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
            yield event.plain_result(
                "用法：/mimo_tts_preview_tagged 你好，这是一个测试"
            )
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
            yield event.chain_result([self._make_record(event, wav_path)])
        except Exception as e:
            logger.exception("[mimo_tts_decorator] mimo_tts_test failed")
            yield event.plain_result(f"MiMo TTS 失败：{e}")

    @filter.on_decorating_result()
    async def on_decorating_result(
        self, event: AstrMessageEvent, *_args, **_kwargs
    ):
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
            logger.warning(
                f"[mimo_tts_decorator] 文本过长，跳过 TTS: {len(text)} > {max_chars}"
            )
            return

        try:
            wav_path = await self._call_mimo_tts(text)
        except Exception as e:
            logger.exception(
                f"[mimo_tts_decorator] TTS failed, keep original text. err={e}"
            )
            return

        mode = self._cfg("trigger_mode", "replace_plain")
        record = self._make_record(event, wav_path)
        if mode == "append_record":
            if self._cfg("preserve_text_when_append", True):
                chain.append(record)
            else:
                result.chain = self._replace_plain_with_record(chain, record)
        else:
            result.chain = self._replace_plain_with_record(chain, record)

        self._log_tts_send_success("decorator", wav_path, text)

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent, *_args, **_kwargs):
        if not self._cfg("cleanup_after_send", True):
            return
        self._cleanup_tracked_event_files(event)

    async def terminate(self):
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("[mimo_tts_decorator] terminate")
