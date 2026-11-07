"""今日运势卡片渲染器。

版式完全对照 koishi-plugin-jrys-prpr 的 render.ts：
  800x800 画布 / 底部半透明面板(圆角 26 + backdrop blur) / 头像 / 日期渐变 /
  总结 / 星级渐变 / 正文 (line-height 1.55, 虚线框) / 页脚
几何值已对着真实 chromium 的 DOM 盒模型校准（误差 < 0.1px）。

在此之上增加了布局调节：
  * 头像开关、位置(靠上/靠左/靠右)、尺寸、偏移
  * 日期 / 运势文字 / 运势星星 的字号与偏移
头像不在顶部时，面板会自动变矮（不再为头像留出整行高度）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFilter, ImageFont

CANVAS = 800
PAD_TOP, PAD_SIDE, PAD_BOTTOM = 30, 26, 16

AVATAR_SIZE = 74
AVATAR_BORDER = 3
AVATAR_MB = 6
AVATAR_SIDE_GAP = 20

DATE_SIZE, DATE_MB = 22, 4
SUMMARY_SIZE, SUMMARY_MB = 38, 2
STAR_SIZE, STAR_MB = 34, 4

BODY_SIZE, BODY_LH = 19, 1.55
BODY_PAD_V, BODY_PAD_H, BODY_MT, BODY_RADIUS = 9, 12, 8, 12

FOOTER_SIZE, FOOTER_MT, FOOTER_OPACITY = 14, 10, 0.85
INFO2_MT = 4

RAINBOW = [
    (252, 181, 181),
    (252, 214, 174),
    (253, 232, 166),
    (195, 247, 177),
    (174, 214, 250),
    (196, 175, 245),
    (241, 175, 204),
]

NO_LINE_START = set("。，、；：？！）】》」』’”,.;:?!)]}>…％%")
NO_LINE_END = set("（【《「『‘“([{<")

# chromium: hhea ascent(1em) + descent(0.28125em)，行盒高向上取整
DESC_RATIO = 0.28125

POSITION_ALIASES = {
    "top": "top",
    "center": "top",
    "上": "top",
    "靠上": "top",
    "顶部": "top",
    "left": "left",
    "左": "left",
    "靠左": "left",
    "左侧": "left",
    "right": "right",
    "右": "right",
    "靠右": "right",
    "右侧": "right",
}


def normalize_position(value, default: str = "top") -> str:
    if value is None:
        return default
    key = str(value).strip().lower()
    return POSITION_ALIASES.get(key, POSITION_ALIASES.get(str(value).strip(), default))


def parse_color(value, default=(255, 255, 255, 255)) -> tuple[int, int, int, int]:
    """接受 RGBA 配置对象 {r,g,b,a}(a 为 0-100 百分比) 或旧的 rgba()/hex 字符串。"""
    if isinstance(value, dict):
        try:
            r = max(0, min(255, int(value.get("r", 0))))
            g = max(0, min(255, int(value.get("g", 0))))
            b = max(0, min(255, int(value.get("b", 0))))
            a_raw = float(value.get("a", 100))
            a = round(max(0.0, min(100.0, a_raw)) * 255 / 100)
            return (r, g, b, a)
        except (TypeError, ValueError):
            return default
    if not value:
        return default
    v = str(value).strip()
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        try:
            if len(h) == 8:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16))
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
        except ValueError:
            return default
    m = re.match(r"rgba?\(([^)]+)\)", v, re.I)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        try:
            r, g, b = (int(float(parts[i])) for i in range(3))
            if len(parts) >= 4:
                a = parts[3]
                alpha = float(a[:-1]) / 100 if a.endswith("%") else float(a)
                return (r, g, b, max(0, min(255, round(alpha * 255))))
            return (r, g, b, 255)
        except (ValueError, IndexError):
            return default
    named = {"white": (255, 255, 255, 255), "black": (0, 0, 0, 255)}
    return named.get(v.lower(), default)


def cover_crop(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """== background-size: cover; background-position: center."""
    tw, th = size
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    iw, ih = img.size
    scale = max(tw / iw, th / ih)
    nw, nh = max(tw, round(iw * scale)), max(th, round(ih * scale))
    img = img.convert("RGB").resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


@dataclass
class Layout:
    """布局调节参数（全部有默认值，默认值 == koishi 原始版式）。"""

    avatar_show: bool = True
    avatar_position: str = "top"  # top / left / right
    avatar_size: int = AVATAR_SIZE
    avatar_offset_x: int = 0
    avatar_offset_y: int = 0

    date_size: int = DATE_SIZE
    date_offset_x: int = 0
    date_offset_y: int = 0

    summary_size: int = SUMMARY_SIZE
    summary_offset_x: int = 0
    summary_offset_y: int = 0

    star_size: int = STAR_SIZE
    star_offset_x: int = 0
    star_offset_y: int = 0

    def __post_init__(self) -> None:
        self.avatar_position = normalize_position(self.avatar_position)
        self.avatar_size = max(16, int(self.avatar_size or AVATAR_SIZE))
        self.date_size = max(8, int(self.date_size or DATE_SIZE))
        self.summary_size = max(8, int(self.summary_size or SUMMARY_SIZE))
        self.star_size = max(8, int(self.star_size or STAR_SIZE))
        self.avatar_show = bool(self.avatar_show)


class CardRenderer:
    def __init__(
        self,
        font_path: str,
        *,
        mask_color: str = "rgba(0,0,0,0.5)",
        mask_blur: float = 10,
        summary_color: str = "rgba(255,255,255,1)",
        star_gradient: bool = True,
        body_color: str = "rgba(255,255,255,1)",
        dash_width: int = 5,
        dash_color: str = "rgba(255, 255, 255, 0.5)",
        layout: Layout | None = None,
    ) -> None:
        self.font_path = font_path
        self.mask_color = parse_color(mask_color)
        self.mask_blur = float(mask_blur)
        self.summary_color = parse_color(summary_color)
        self.star_gradient = bool(star_gradient)
        self.body_color = parse_color(body_color)
        self.dash_width = max(0, int(dash_width))
        self.dash_color = parse_color(dash_color)
        self.layout = layout or Layout()
        self._fonts: dict[int, ImageFont.FreeTypeFont] = {}

    # ---------- 字体度量 ----------
    def font(self, size: int) -> ImageFont.FreeTypeFont:
        if size not in self._fonts:
            self._fonts[size] = ImageFont.truetype(self.font_path, size)
        return self._fonts[size]

    @staticmethod
    def line_height(size: int, ratio: float | None = None) -> float:
        if ratio is not None:
            return size * ratio
        return math.ceil(size * (1 + DESC_RATIO))

    def text_top(self, block_y: float, size: int, lh: float) -> float:
        ascent = self.font(size).getmetrics()[0]
        baseline = block_y + (lh - size * (1 + DESC_RATIO)) / 2 + size
        return baseline - ascent

    def advance(self, text: str, size: int) -> float:
        return self.font(size).getlength(text)

    # ---------- 排版 ----------
    def wrap(self, text: str, size: int, max_width: float) -> list[str]:
        text = (text or "").replace("\r", "").replace("\n", "")
        if not text:
            return [""]
        lines: list[str] = []
        cur = ""
        for ch in text:
            test = cur + ch
            if self.advance(test, size) <= max_width or not cur:
                cur = test
                continue
            if ch in NO_LINE_START:
                lines.append(cur[:-1])
                cur = cur[-1] + ch
            elif cur[-1] in NO_LINE_END:
                lines.append(cur[:-1])
                cur = cur[-1] + ch
            else:
                lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
        return lines

    # ---------- 绘制 ----------
    def _draw_text(
        self,
        target: Image.Image,
        text: str,
        size: int,
        top: float,
        color: tuple[int, int, int, int],
        center: bool = True,
        x: float | None = None,
    ) -> None:
        if not text:
            return
        if x is None:
            x = (CANVAS - self.advance(text, size)) / 2 if center else 0
        ImageDraw.Draw(target).text((x, top), text, font=self.font(size), fill=color)

    def _draw_gradient_text(
        self, target: Image.Image, text: str, size: int, top: float, offset_x: float = 0
    ) -> None:
        if not text:
            return
        width = int(math.ceil(self.advance(text, size)))
        height = int(math.ceil(size * 1.6))
        if width <= 0:
            return
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).text((0, 0), text, font=self.font(size), fill=255)

        grad = Image.new("RGBA", (width, height))
        gd = ImageDraw.Draw(grad)
        n = len(RAINBOW)
        for x in range(width):
            pos = (x / max(width - 1, 1)) * (n - 1)
            i = min(int(pos), n - 2)
            f = pos - i
            c0, c1 = RAINBOW[i], RAINBOW[i + 1]
            gd.line(
                [(x, 0), (x, height)],
                fill=tuple(round(c0[k] + (c1[k] - c0[k]) * f) for k in range(3)) + (255,),
            )
        target.paste(grad, (int(round((CANVAS - width) / 2 + offset_x)), int(round(top))), mask)

    def _draw_justified(
        self,
        target: Image.Image,
        lines: list[str],
        size: int,
        box_x: float,
        box_y: float,
        content_width: float,
        line_height: float,
    ) -> None:
        for idx, line in enumerate(lines):
            top = self.text_top(box_y + idx * line_height, size, line_height)
            if idx == len(lines) - 1 or len(line) < 2:
                self._draw_text(target, line, size, top, self.body_color, center=False, x=box_x)
                continue
            natural = self.advance(line, size)
            gap = (content_width - natural) / (len(line) - 1)
            cursor = box_x
            for ch in line:
                self._draw_text(target, ch, size, top, self.body_color, center=False, x=cursor)
                cursor += self.advance(ch, size) + gap

    def _dashed_round_rect(
        self,
        target: Image.Image,
        box: tuple[float, float, float, float],
        radius: float,
        width: int,
        color: tuple[int, int, int, int],
        dash: float | None = None,
        gap: float | None = None,
    ) -> None:
        if width <= 0:
            return
        # Chromium 的 dashed 边框：划长 = 2×线宽，空隙 = 1×线宽，整体画在边框盒内侧
        dash = 2.0 * width if dash is None else dash
        gap = 1.0 * width if gap is None else gap
        x0, y0, x1, y1 = box
        inset = width / 2
        pts = self._round_rect_points(
            x0 + inset, y0 + inset, x1 - inset, y1 - inset, max(radius - inset, 0)
        )
        layer = Image.new("RGBA", target.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        travelled, drawing, prev = 0.0, True, pts[0]
        for pt in pts[1:]:
            seg = math.dist(prev, pt)
            if seg == 0:
                continue
            steps = max(1, int(seg))
            for s in range(steps):
                travelled += seg / steps
                if drawing:
                    a = (prev[0] + (pt[0] - prev[0]) * s / steps, prev[1] + (pt[1] - prev[1]) * s / steps)
                    b = (prev[0] + (pt[0] - prev[0]) * (s + 1) / steps, prev[1] + (pt[1] - prev[1]) * (s + 1) / steps)
                    d.line([a, b], fill=color, width=width)
                if travelled >= (dash if drawing else gap):
                    travelled, drawing = 0.0, not drawing
            prev = pt
        target.alpha_composite(layer)

    @staticmethod
    def _round_rect_points(x0: float, y0: float, x1: float, y1: float, r: float) -> list[tuple[float, float]]:
        r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
        pts: list[tuple[float, float]] = []
        for (cx, cy), a0, a1 in [
            ((x0 + r, y0 + r), 180, 270),
            ((x1 - r, y0 + r), 270, 360),
            ((x1 - r, y1 - r), 0, 90),
            ((x0 + r, y1 - r), 90, 180),
        ]:
            steps = max(2, int(abs(a1 - a0) / 6))
            for i in range(steps + 1):
                a = math.radians(a0 + (a1 - a0) * i / steps)
                pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        pts.append(pts[0])
        return pts

    def _draw_avatar(self, canvas: Image.Image, avatar: Image.Image, x: int, y: int, size: int) -> None:
        border = max(1, round(AVATAR_BORDER * size / AVATAR_SIZE))
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).ellipse((x - 2, y, x + size + 2, y + size + 4), fill=(0, 0, 0, 64))
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(8)))

        inner = max(1, size - border * 2)
        face = cover_crop(avatar, (inner, inner))
        mask = Image.new("L", (inner, inner), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, inner - 1, inner - 1), fill=255)

        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        layer.paste(face, (x + border, y + border), mask)
        ImageDraw.Draw(layer).ellipse(
            (x, y, x + size - 1, y + size - 1),
            outline=(255, 255, 255, 191),
            width=border,
        )
        canvas.alpha_composite(layer)

    # ---------- 主入口 ----------
    def render(
        self,
        *,
        background: Image.Image,
        avatar: Image.Image | None,
        date_text: str,
        fortune_summary: str,
        lucky_star: str,
        sign_text: str,
        unsign_text: str,
        footer_text: str = "仅供娱乐 | 相信科学 | 请勿迷信",
    ) -> Image.Image:
        lay = self.layout
        content_w = CANVAS - PAD_SIDE * 2
        box_text_w = content_w - BODY_PAD_H * 2 - self.dash_width * 2

        sign_lines = self.wrap(sign_text, BODY_SIZE, box_text_w)
        unsign_lines = self.wrap(unsign_text, BODY_SIZE, box_text_w)

        lh_body = BODY_SIZE * BODY_LH
        h_date = self.line_height(lay.date_size)
        h_sum = self.line_height(lay.summary_size)
        h_star = self.line_height(lay.star_size)
        h_foot = self.line_height(FOOTER_SIZE)
        h_sign = len(sign_lines) * lh_body + BODY_PAD_V * 2 + self.dash_width * 2
        h_unsign = len(unsign_lines) * lh_body + BODY_PAD_V * 2 + self.dash_width * 2

        h_info1 = h_date + DATE_MB + h_sum + SUMMARY_MB + h_star + STAR_MB
        h_info2 = INFO2_MT + BODY_MT + h_sign + BODY_MT + h_unsign + FOOTER_MT + h_foot

        avatar_row = (
            (max(lay.avatar_size, AVATAR_SIZE) + AVATAR_MB)
            if lay.avatar_show and lay.avatar_position == "top"
            else 0
        )

        overlay_h = PAD_TOP + avatar_row + h_info1 + h_info2 + PAD_BOTTOM
        overlay_top = CANVAS - overlay_h

        canvas = cover_crop(background, (CANVAS, CANVAS)).convert("RGBA")

        # 面板：backdrop blur + 半透明蒙版 + 顶部圆角
        panel_mask = Image.new("L", (CANVAS, CANVAS), 0)
        ImageDraw.Draw(panel_mask).rounded_rectangle(
            (0, round(overlay_top), CANVAS - 1, CANVAS - 1),
            radius=26,
            fill=255,
            corners=(True, True, False, False),
        )
        if self.mask_blur > 0:
            canvas = Image.composite(
                canvas.filter(ImageFilter.GaussianBlur(self.mask_blur)), canvas, panel_mask
            )
        r, g, b, a = self.mask_color
        shade = Image.new("RGBA", (CANVAS, CANVAS), (r, g, b, 0))
        shade.putalpha(panel_mask.point(lambda p: int(p * a / 255)))
        canvas = Image.alpha_composite(canvas, shade)

        info1_top = overlay_top + PAD_TOP + avatar_row

        # 头像（顶部流内 / 左右悬浮）
        if lay.avatar_show and avatar is not None:
            if lay.avatar_position == "top":
                ax = (CANVAS - lay.avatar_size) // 2
                ay = overlay_top + PAD_TOP
            else:
                ax = PAD_SIDE if lay.avatar_position == "left" else CANVAS - PAD_SIDE - lay.avatar_size
                ay = info1_top + (h_info1 - lay.avatar_size) / 2
            ax += lay.avatar_offset_x
            ay += lay.avatar_offset_y
            self._draw_avatar(canvas, avatar, int(round(ax)), int(round(ay)), lay.avatar_size)

        y = info1_top

        self._draw_gradient_text(
            canvas,
            date_text,
            lay.date_size,
            self.text_top(y + lay.date_offset_y, lay.date_size, h_date),
            lay.date_offset_x,
        )
        y += h_date + DATE_MB

        self._draw_text(
            canvas,
            fortune_summary,
            lay.summary_size,
            self.text_top(y + lay.summary_offset_y, lay.summary_size, h_sum),
            self.summary_color,
            x=(CANVAS - self.advance(fortune_summary, lay.summary_size)) / 2 + lay.summary_offset_x
            if fortune_summary
            else None,
        )
        y += h_sum + SUMMARY_MB

        if self.star_gradient:
            self._draw_gradient_text(
                canvas,
                lucky_star,
                lay.star_size,
                self.text_top(y + lay.star_offset_y, lay.star_size, h_star),
                lay.star_offset_x,
            )
        else:
            self._draw_text(
                canvas,
                lucky_star,
                lay.star_size,
                self.text_top(y + lay.star_offset_y, lay.star_size, h_star),
                (255, 255, 255, 255),
                x=(CANVAS - self.advance(lucky_star, lay.star_size)) / 2 + lay.star_offset_x
                if lucky_star
                else None,
            )
        y += h_star + STAR_MB + INFO2_MT

        y += BODY_MT
        self._dashed_round_rect(
            canvas,
            (PAD_SIDE, y, PAD_SIDE + content_w, y + h_sign),
            BODY_RADIUS,
            self.dash_width,
            self.dash_color,
        )
        self._draw_justified(
            canvas,
            sign_lines,
            BODY_SIZE,
            PAD_SIDE + self.dash_width + BODY_PAD_H,
            y + self.dash_width + BODY_PAD_V,
            box_text_w,
            lh_body,
        )
        y += h_sign + BODY_MT

        self._dashed_round_rect(
            canvas,
            (PAD_SIDE, y, PAD_SIDE + content_w, y + h_unsign),
            BODY_RADIUS,
            self.dash_width,
            self.dash_color,
        )
        self._draw_justified(
            canvas,
            unsign_lines,
            BODY_SIZE,
            PAD_SIDE + self.dash_width + BODY_PAD_H,
            y + self.dash_width + BODY_PAD_V,
            box_text_w,
            lh_body,
        )
        y += h_unsign + FOOTER_MT

        footer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        self._draw_text(footer, footer_text, FOOTER_SIZE, self.text_top(y, FOOTER_SIZE, h_foot), self.body_color)
        footer.putalpha(footer.getchannel("A").point(lambda p: int(p * FOOTER_OPACITY)))
        canvas = Image.alpha_composite(canvas, footer)

        return canvas.convert("RGB")
