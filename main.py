"""今日运势 —— AstrBot 插件（Koishi 版式复刻）

版式、抽签算法、背景图匹配规则均与 koishi-plugin-jrys-prpr 保持一致：
  * 800x800 卡片、底部半透明面板、渐变日期/星级、虚线框正文
  * 按运势档位(luckValue)选择对应背景图（大吉/中吉/吉/吉带凶/吉凶参半/凶带吉/小凶/凶）
  * 同一天（中国时区）同一用户运势固定，与 koishi 抽到同一档位
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path

import aiohttp
from PIL import Image

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image as AtImage
from astrbot.api.star import Context, Star, register

from .fortune import (
    LUCK_TO_IMAGE,
    china_date_string,
    china_formatted_date,
    draw_luck,
    load_jrys_json,
    normalize_weights,
    pick_entry,
)
from .renderer import AVATAR_SIZE, DATE_SIZE, SUMMARY_SIZE, STAR_SIZE, CardRenderer, Layout

PLUGIN_DIR = Path(__file__).resolve().parent
DEFAULT_FONT = "JINGNANBOBOHEI-BOLD-2.TTF"
DEFAULT_HINT = "正在分析你的运势哦~请稍等~~"


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _runtime_dir() -> Path:
    """运行时数据目录（头像缓存、卡片、每日次数）。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = Path(str(get_astrbot_data_path())) / "plugin_data" / "astrbot_plugin_jrysprpr"
    except Exception:  # noqa: BLE001
        base = PLUGIN_DIR / "runtime"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path("/tmp/astrbot_plugin_jrysprpr")
        base.mkdir(parents=True, exist_ok=True)
    return base


@register("astrbot_plugin_jrysprpr", "ominus", "今日运势卡片（Koishi 版式复刻）", "2.2.0")
class JrysPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._jrys_data: dict | None = None
        self._session: aiohttp.ClientSession | None = None
        self._counts_lock = asyncio.Lock()
        self._runtime = _runtime_dir()
        self._avatar_dir = self._runtime / "avatars"
        self._card_dir = self._runtime / "cards"
        self._counts_path = self._runtime / "daily_counts.json"
        for path in (self._avatar_dir, self._card_dir):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    # ---------------- 配置读取 ----------------
    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default) if hasattr(self.config, "get") else default
        return default if value is None else value

    @property
    def command_names(self) -> set[str]:
        base = str(self._cfg("command", "jrys") or "jrys").strip().lstrip("/")
        return {n for n in {base, "jrys", "今日运势", "运势"} if n}

    @property
    def html_setting(self) -> dict:
        value = self._cfg("HTML_setting", {})
        return value if isinstance(value, dict) else {}

    @property
    def layout_setting(self) -> dict:
        value = self._cfg("layout", {})
        return value if isinstance(value, dict) else {}

    def _layout(self) -> Layout:
        cfg = self.layout_setting
        return Layout(
            avatar_show=_as_bool(cfg.get("avatar_show", True), True),
            avatar_position=str(cfg.get("avatar_position", "top") or "top"),
            avatar_size=_as_int(cfg.get("avatar_size"), AVATAR_SIZE),
            avatar_offset_x=_as_int(cfg.get("avatar_offset_x"), 0),
            avatar_offset_y=_as_int(cfg.get("avatar_offset_y"), 0),
            date_size=_as_int(cfg.get("date_size"), DATE_SIZE),
            date_offset_x=_as_int(cfg.get("date_offset_x"), 0),
            date_offset_y=_as_int(cfg.get("date_offset_y"), 0),
            summary_size=_as_int(cfg.get("summary_size"), SUMMARY_SIZE),
            summary_offset_x=_as_int(cfg.get("summary_offset_x"), 0),
            summary_offset_y=_as_int(cfg.get("summary_offset_y"), 0),
            star_size=_as_int(cfg.get("star_size"), STAR_SIZE),
            star_offset_x=_as_int(cfg.get("star_offset_x"), 0),
            star_offset_y=_as_int(cfg.get("star_offset_y"), 0),
        )

    def _renderer(self) -> CardRenderer:
        font_name = str(self._cfg("font_name", DEFAULT_FONT) or DEFAULT_FONT)
        font_path = PLUGIN_DIR / "font" / font_name
        if not font_path.exists():
            fallback = PLUGIN_DIR / "font" / DEFAULT_FONT
            font_path = fallback if fallback.exists() else font_path
        hs = self.html_setting
        return CardRenderer(
            str(font_path),
            mask_color=hs.get("MaskColor", "rgba(0,0,0,0.5)"),
            mask_blur=float(hs.get("Maskblurs", 10) or 0),
            summary_color=hs.get("HoroscopeTextColor", "rgba(255,255,255,1)"),
            star_gradient=_as_bool(hs.get("luckyStarGradientColor", True), True),
            body_color=hs.get("HoroscopeDescriptionTextColor", "rgba(255,255,255,1)"),
            dash_width=_as_int(hs.get("DashedboxThickn"), 5),
            dash_color=hs.get("Dashedboxcolor", "rgba(255, 255, 255, 0.5)"),
            layout=self._layout(),
        )

    def _weights(self) -> dict[int, int]:
        raw = self._cfg("fortune_weights", None)
        return normalize_weights(raw if isinstance(raw, dict) else None)

    # ---------------- 数据与资源 ----------------
    def _jrys(self) -> dict:
        if self._jrys_data is None:
            self._jrys_data = load_jrys_json(PLUGIN_DIR / "jrys.json")
        return self._jrys_data

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                connector=aiohttp.TCPConnector(limit=10),
            )
        return self._session

    async def _fetch_avatar(self, user_id: str) -> Image.Image | None:
        if not user_id or not str(user_id).isdigit():
            return None
        path = self._avatar_dir / f"{user_id}.jpg"
        expire = _as_int(self._cfg("avatar_cache_expiration"), 86400)
        try:
            if path.exists() and time.time() - path.stat().st_mtime < expire:
                data = path.read_bytes()
            else:
                session = await self._http()
                url = f"http://q.qlogo.cn/g?b=qq&nk={user_id}&s=640"
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    data = await resp.read()
                try:
                    path.write_bytes(data)
                except OSError:
                    pass
            return Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"获取头像失败({user_id}): {exc}")
            if path.exists():
                try:
                    return Image.open(path).convert("RGB")
                except Exception:  # noqa: BLE001
                    return None
            return None

    def _background_path(self, luck: int) -> Path:
        name = LUCK_TO_IMAGE.get(luck, "吉")
        folder = PLUGIN_DIR / "backgroundFolder"
        target = folder / f"{name}.jpg"
        if target.exists():
            return target
        for candidate in sorted(folder.glob("*.jpg")):
            return candidate
        raise FileNotFoundError(f"背景图缺失: {folder}")

    # ---------------- 每日次数限制 ----------------
    def _load_counts(self) -> dict:
        try:
            data = json.loads(self._counts_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_counts(self, data: dict) -> None:
        try:
            self._counts_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning(f"保存每日次数失败: {exc}")

    async def _consume_quota(self, user_id: str) -> tuple[bool, int, int]:
        """返回 (是否允许, 已用次数, 上限)。上限 <= 0 表示不限制。"""
        limit = _as_int(self._cfg("daily_draw_limit", 1), 1)
        if limit <= 0:
            return True, 0, 0
        today = china_date_string()
        async with self._counts_lock:
            data = self._load_counts()
            day = data.get(today)
            if not isinstance(day, dict):
                # 跨天清理，只保留当天
                data = {today: {}}
                day = data[today]
            used = _as_int(day.get(user_id), 0)
            if used >= limit:
                return False, used, limit
            day[user_id] = used + 1
            self._save_counts(data)
            return True, used, limit

    # ---------------- 渲染 ----------------
    def _render_card(self, entry: dict, luck: int, avatar: Image.Image | None) -> Image.Image:
        renderer = self._renderer()
        background = Image.open(self._background_path(luck))
        background.load()
        return renderer.render(
            background=background,
            avatar=avatar,
            date_text=china_formatted_date(),
            fortune_summary=entry.get("fortuneSummary", ""),
            lucky_star=entry.get("luckyStar", ""),
            sign_text=entry.get("signText", ""),
            unsign_text=entry.get("unsignText", ""),
        )

    async def _build(self, user_id: str) -> tuple[Path, dict, int]:
        weights = self._weights()
        luck = draw_luck(weights, user_id)
        entry = pick_entry(self._jrys(), luck, user_id)
        avatar = await self._fetch_avatar(user_id)
        card = await asyncio.to_thread(self._render_card, entry, luck, avatar)
        out_path = self._card_dir / f"{user_id}_{int(time.time() * 1000)}.png"
        card.save(out_path, format="PNG", optimize=True)
        logger.info(f"今日运势: user={user_id} luck={luck} -> {out_path.name}")
        return out_path, entry, luck

    # ---------------- 消息构造 ----------------
    def _reply_image(self, event: AstrMessageEvent, image_path: str):
        """群聊里发图时顺带 @ 发送者。"""
        user_id = str(event.get_sender_id() or "")
        in_group = bool(event.get_group_id())
        if in_group and user_id and _as_bool(self._cfg("at_sender", True), True):
            return event.chain_result([At(qq=user_id), AtImage.fromFileSystem(str(image_path))])
        return event.image_result(str(image_path))

    # ---------------- 事件处理 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def jrys_handler(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if not text:
            return
        split = False
        body = text
        for suffix in (" -s", "-s", " --split", "--split"):
            if body.endswith(suffix):
                split = True
                body = body[: -len(suffix)].strip()
                break
        body = body.lstrip("/").strip()
        lowered = body.lower()

        if lowered in {"原图", "获取原图", "查看运势背景图", "jrys_last", "运势原图"}:
            event.stop_event()
            user_id = str(event.get_sender_id() or "")
            try:
                luck = draw_luck(self._weights(), user_id)
                yield event.plain_result(f"今日运势对应的背景图（{LUCK_TO_IMAGE.get(luck, '吉')}）")
                yield self._reply_image(event, str(self._background_path(luck)))
            except Exception as exc:  # noqa: BLE001
                logger.error(f"发送原图失败: {exc}")
                yield event.plain_result("获取原图失败了，稍后再试试～")
            return

        if lowered not in {name.lower() for name in self.command_names}:
            return
        if not _as_bool(self._cfg("jrys_keyword_enabled", True), True) and not self._is_command_call(text):
            return

        event.stop_event()
        user_id = str(event.get_sender_id() or "")

        allowed, used, limit = await self._consume_quota(user_id)
        if not allowed:
            yield event.plain_result(
                f"今天已经抽过 {used} 次啦（每日上限 {limit} 次），明天再来吧～"
            )
            return

        hint = self._cfg("Checkin_HintText", DEFAULT_HINT)
        if hint and str(hint).strip().lower() != "unset":
            yield event.plain_result(str(hint))

        try:
            if split:
                luck = draw_luck(self._weights(), user_id)
                entry = pick_entry(self._jrys(), luck, user_id)
                yield self._reply_image(event, str(self._background_path(luck)))
                yield event.plain_result(
                    f"{entry.get('fortuneSummary', '')}\n{entry.get('luckyStar', '')}\n\n"
                    f"{entry.get('signText', '')}\n\n{entry.get('unsignText', '')}"
                )
                return

            out_path, _entry, _luck = await self._build(user_id)
            yield self._reply_image(event, str(out_path))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"生成今日运势失败: {exc}", exc_info=True)
            yield event.plain_result(f"渲染失败了：{exc}")

    def _is_command_call(self, text: str) -> bool:
        raw = (text or "").strip()
        if not raw.startswith(("/", "／")):
            return False
        name = raw.lstrip("/／").split(" ")[0].strip().lower()
        return name in {n.lower() for n in self.command_names}

    async def terminate(self):
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("今日运势插件 HTTP 会话已关闭")
