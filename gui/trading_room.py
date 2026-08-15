"""高清交易室 — 贴图背景 + 6 机器人精灵，Canvas 叠加投票气泡。"""

from __future__ import annotations

import math
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import tkinter as tk

logger = logging.getLogger(__name__)

try:
    from PIL import Image, ImageTk, ImageFilter, ImageDraw
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


PALETTE = {
    "floor": "#C9B896",
    "floor_dark": "#B09A78",
    "wall": "#8B7355",
    "wall_light": "#A89070",
    "table": "#6B4423",
    "table_top": "#8B5A2B",
    "table_edge": "#4A2F1A",
    "wood": "#5D4037",
    "paper": "#F5E6C8",
    "ink": "#3E2723",
    "lamp": "#D4A017",
    "plant": "#2E7D32",
    "rose": "#C2185B",
    "screen": "#1A237E",
    "screen_glow": "#3949AB",
}

TRADER_DEFS: List[Dict[str, Any]] = [
    {"id": "momentum", "name": "趋势猎手", "emoji": "🚀", "body": "#E65100", "accent": "#FFB74D"},
    {"id": "mean_reversion", "name": "均值回归", "emoji": "🔄", "body": "#6A1B9A", "accent": "#CE93D8"},
    {"id": "macro", "name": "宏观情绪", "emoji": "🌍", "body": "#1565C0", "accent": "#90CAF9"},
    {"id": "structure", "name": "结构派", "emoji": "📐", "body": "#2E7D32", "accent": "#A5D6A7"},
    {"id": "flow", "name": "资金流", "emoji": "🐋", "body": "#00838F", "accent": "#80DEEA"},
    {"id": "contrarian", "name": "反共识", "emoji": "🎭", "body": "#C62828", "accent": "#EF9A9A"},
]

_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "trading_room"


def _remove_light_bg(img: "Image.Image", threshold: int = 235) -> "Image.Image":
    """去掉生成图常见的浅色底，得到带透明通道的精灵。"""
    img = img.convert("RGBA")
    pixels = img.load()
    w, h = img.size
    corners = [pixels[0, 0], pixels[w - 1, 0], pixels[0, h - 1], pixels[w - 1, h - 1]]
    br = sum(c[0] for c in corners) // 4
    bg = sum(c[1] for c in corners) // 4
    bb = sum(c[2] for c in corners) // 4
    for y in range(h):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            dist = abs(r - br) + abs(g - bg) + abs(b - bb)
            bright = (r + g + b) / 3
            if bright >= threshold or dist < 55:
                if bright >= threshold - 15 and dist < 90:
                    pixels[x, y] = (r, g, b, max(0, int(a * (threshold - bright) / 20)))
                else:
                    pixels[x, y] = (r, g, b, 0)
    return img


def _autocrop_alpha(img: "Image.Image", pad: int = 8) -> "Image.Image":
    bbox = img.getbbox()
    if not bbox:
        return img
    l, t, r, b = bbox
    l = max(0, l - pad)
    t = max(0, t - pad)
    r = min(img.width, r + pad)
    b = min(img.height, b + pad)
    return img.crop((l, t, r, b))


class TradingRoomCanvas(tk.Canvas):
    """高清书房交易室：石地木桌贴图 + 六机器人精灵围坐。"""

    def __init__(
        self,
        master,
        *,
        on_trader_click: Optional[Callable[[str], None]] = None,
        **kwargs,
    ):
        kwargs.setdefault("bg", PALETTE["floor"])
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, **kwargs)
        self.on_trader_click = on_trader_click
        self._trader_hitboxes: Dict[str, Tuple[int, int, int, int]] = {}
        self._votes: Dict[str, Dict[str, Any]] = {}
        self._selected: Optional[str] = None
        self._anim_phase = 0

        self._bg_photo: Optional[tk.PhotoImage] = None
        self._robot_photos: Dict[str, tk.PhotoImage] = {}
        self._bg_src = None
        self._robot_src: Dict[str, Any] = {}
        self._last_size: Tuple[int, int] = (0, 0)
        self._hd_ready = False
        self._quote: Dict[str, Any] = {
            "symbol": "BNBUSDT",
            "price": None,
            "change_pct": None,
        }

        self._load_assets()
        self.bind("<Configure>", self._on_resize)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)
        self._draw()
        self.after(200, self._tick)

    def _load_assets(self) -> None:
        if not HAS_PIL:
            logger.warning("Pillow 未安装，回退矢量绘制")
            return
        d = _ASSETS
        bg_path = d / "bg.png"
        if bg_path.exists():
            try:
                self._bg_src = Image.open(bg_path).convert("RGB")
            except Exception as e:
                logger.warning("load bg failed: %s", e)

        for t in TRADER_DEFS:
            path = d / f"{t['id']}.png"
            cache = d / f"{t['id']}_sprite.png"
            if not path.exists() and not cache.exists():
                continue
            try:
                if cache.exists():
                    cleaned = Image.open(cache).convert("RGBA")
                else:
                    raw = Image.open(path).convert("RGBA")
                    # 先缩小再抠图，避免启动卡顿
                    raw.thumbnail((640, 640), Image.Resampling.LANCZOS)
                    cleaned = _autocrop_alpha(_remove_light_bg(raw))
                    cleaned.thumbnail((512, 512), Image.Resampling.LANCZOS)
                    try:
                        cleaned.save(cache, "PNG")
                    except Exception:
                        pass
                self._robot_src[t["id"]] = cleaned
            except Exception as e:
                logger.warning("load robot %s failed: %s", t["id"], e)

        self._hd_ready = bool(self._bg_src) and len(self._robot_src) >= 3
        if self._hd_ready:
            logger.info("HD trading room assets loaded (%d robots)", len(self._robot_src))

    def set_votes(self, votes: Dict[str, Dict[str, Any]]) -> None:
        self._votes = dict(votes or {})
        self._draw()

    def set_quote(
        self,
        symbol: str,
        price: Optional[float],
        change_pct: Optional[float] = None,
    ) -> None:
        """更新中间电脑屏幕实时价（只重绘行情层，不全量刷背景）。"""
        try:
            p = float(price) if price is not None else None
            if p is not None and p <= 0:
                p = None
        except (TypeError, ValueError):
            p = None
        try:
            chg = float(change_pct) if change_pct is not None else None
        except (TypeError, ValueError):
            chg = None
        self._quote = {
            "symbol": (symbol or "BNBUSDT").upper(),
            "price": p,
            "change_pct": chg,
        }
        try:
            self._draw_monitor_quote()
        except Exception:
            pass

    def select_trader(self, trader_id: Optional[str]) -> None:
        self._selected = trader_id
        self._draw()

    def _tick(self) -> None:
        self._anim_phase = (self._anim_phase + 1) % 48
        try:
            self._draw_overlays_only()
        except Exception:
            pass
        self.after(220, self._tick)

    def _on_resize(self, _event=None) -> None:
        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)
        if abs(w - self._last_size[0]) < 4 and abs(h - self._last_size[1]) < 4:
            return
        self._last_size = (w, h)
        self._draw()

    def _on_click(self, event) -> None:
        tid = self._hit_test(event.x, event.y)
        if tid and self.on_trader_click:
            self._selected = tid
            self._draw()
            self.on_trader_click(tid)

    def _on_motion(self, event) -> None:
        tid = self._hit_test(event.x, event.y)
        self.configure(cursor="hand2" if tid else "")

    def _hit_test(self, x: int, y: int) -> Optional[str]:
        for tid, (x0, y0, x1, y1) in self._trader_hitboxes.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return tid
        return None

    def _draw(self) -> None:
        self.delete("all")
        self._trader_hitboxes.clear()
        w = max(self.winfo_width(), 320)
        h = max(self.winfo_height(), 280)
        if self._hd_ready:
            self._draw_hd(w, h)
        else:
            self._draw_fallback(w, h)
        self._draw_monitor_quote()
        self._draw_overlays_only()

    @staticmethod
    def _fmt_price(price: float) -> str:
        if price >= 1000:
            return f"{price:,.2f}"
        if price >= 100:
            return f"{price:.2f}"
        if price >= 1:
            return f"{price:.4f}"
        return f"{price:.6f}"

    def _monitor_center(self, w: int, h: int) -> Tuple[int, int, int, int]:
        """返回圆桌中间电脑屏中心与半宽半高 (cx, cy, hw, hh)。"""
        # 贴图里屏约在桌心偏上；机器人围坐圆心约 0.55
        cx, cy = w // 2, int(h * 0.52)
        hw = max(52, int(w * 0.10))
        hh = max(34, int(h * 0.08))
        return cx, cy, hw, hh

    def _draw_monitor_quote(self) -> None:
        """在桌子中间电脑屏幕上绘制所选币种实时价。"""
        self.delete("monitor_quote")
        w = max(self.winfo_width(), 320)
        h = max(self.winfo_height(), 280)
        cx, cy, hw, hh = self._monitor_center(w, h)
        q = self._quote
        symbol = str(q.get("symbol") or "—")
        price = q.get("price")
        change_pct = q.get("change_pct")

        # 暗色屏面，确保贴图背景上字可读
        self.create_rectangle(
            cx - hw, cy - hh, cx + hw, cy + hh,
            fill="#0D1117", outline="#37474F", width=2,
            tags="monitor_quote",
        )
        # 顶边高光（呼应像素屏框）
        self.create_line(
            cx - hw + 3, cy - hh + 2, cx + hw - 3, cy - hh + 2,
            fill="#78909C", width=1, tags="monitor_quote",
        )

        short = symbol.replace("USDT", "") if symbol.endswith("USDT") else symbol
        self.create_text(
            cx, cy - hh // 2 + 4,
            text=short,
            fill="#80CBC4",
            font=("Courier New", 9, "bold"),
            tags="monitor_quote",
        )

        if price is None:
            price_txt = "…"
            price_color = "#90A4AE"
        else:
            price_txt = self._fmt_price(float(price))
            price_color = "#E8F5E9"

        font_sz = 12 if len(price_txt) <= 10 else 10
        self.create_text(
            cx, cy + 2,
            text=price_txt,
            fill=price_color,
            font=("Courier New", font_sz, "bold"),
            tags="monitor_quote",
        )

        if change_pct is None:
            chg_txt = "24h —"
            chg_color = "#78909C"
        else:
            chg = float(change_pct)
            sign = "+" if chg >= 0 else ""
            chg_txt = f"{sign}{chg:.2f}%"
            chg_color = "#66BB6A" if chg >= 0 else "#EF5350"
        self.create_text(
            cx, cy + hh // 2 - 4,
            text=chg_txt,
            fill=chg_color,
            font=("Courier New", 8, "bold"),
            tags="monitor_quote",
        )

    def _draw_hd(self, w: int, h: int) -> None:
        assert self._bg_src is not None
        bg = self._bg_src.copy()
        bw, bh = bg.size
        scale = max(w / bw, h / bh)
        nw, nh = max(1, int(bw * scale)), max(1, int(bh * scale))
        bg = bg.resize((nw, nh), Image.Resampling.LANCZOS)
        left = (nw - w) // 2
        top = (nh - h) // 2
        bg = bg.crop((left, top, left + w, top + h))

        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        for i in range(36):
            alpha = int(i * 1.1)
            draw.rectangle([i, i, w - 1 - i, h - 1 - i], outline=(40, 25, 15, alpha))
        bg_rgba = Image.alpha_composite(bg.convert("RGBA"), overlay)

        self._bg_photo = ImageTk.PhotoImage(bg_rgba.convert("RGB"))
        self.create_image(0, 0, anchor=tk.NW, image=self._bg_photo, tags="bg")

        self.create_rectangle(0, 0, w, 36, fill="#5D4037", outline="")
        self.create_text(
            w // 2, 18,
            text="BNB 交易员议会 · 高清圆桌议事厅",
            fill="#F5E6C8", font=("Microsoft YaHei UI", 11, "bold"),
        )

        cx, cy = w // 2, int(h * 0.55)
        radius_x = int(w * 0.34)
        radius_y = int(h * 0.28)
        robot_h = max(72, min(140, int(h * 0.22)))

        n = len(TRADER_DEFS)
        self._robot_photos.clear()
        for i, t in enumerate(TRADER_DEFS):
            angle = -math.pi / 2 + i * (2 * math.pi / n)
            x = int(cx + radius_x * math.cos(angle))
            y = int(cy + radius_y * math.sin(angle))
            self._blit_robot(t, x, y, robot_h, selected=(t["id"] == self._selected))
            vote = self._votes.get(t["id"])
            if vote:
                self._draw_speech(x, y - robot_h // 2 - 28, vote)

    def _blit_robot(
        self,
        t: Dict[str, Any],
        x: int,
        y: int,
        target_h: int,
        *,
        selected: bool,
    ) -> None:
        tid = t["id"]
        src = self._robot_src.get(tid)
        if src is None:
            self._draw_vector_robot(x, y, t, selected=selected)
            return

        ratio = target_h / max(1, src.height)
        tw = max(1, int(src.width * ratio))
        th = max(1, int(src.height * ratio))
        sprite = src.resize((tw, th), Image.Resampling.LANCZOS)
        ox = oy = 0

        if selected:
            glow = Image.new("RGBA", (tw + 16, th + 16), (0, 0, 0, 0))
            glow.paste(sprite, (8, 8), sprite)
            glow = glow.filter(ImageFilter.GaussianBlur(3))
            yellow = Image.new("RGBA", glow.size, (255, 213, 79, 90))
            mask = glow.split()[-1]
            glow_tint = Image.composite(yellow, Image.new("RGBA", glow.size, (0, 0, 0, 0)), mask)
            canvas = Image.new("RGBA", glow.size, (0, 0, 0, 0))
            canvas = Image.alpha_composite(canvas, glow_tint)
            canvas.paste(sprite, (8, 8), sprite)
            sprite = canvas
            ox = oy = 8

        photo = ImageTk.PhotoImage(sprite)
        self._robot_photos[tid] = photo
        self.create_image(x, y, image=photo, tags=("robot", tid))

        self.create_rectangle(
            x - 42, y + th // 2 - oy + 2,
            x + 42, y + th // 2 - oy + 20,
            fill="#3E2723", outline="#8D6E63", width=1,
        )
        self.create_text(
            x, y + th // 2 - oy + 11,
            text=f"{t['emoji']} {t['name']}",
            fill="#F5E6C8", font=("Microsoft YaHei UI", 8, "bold"),
        )

        half_w = tw // 2 + 4
        half_h = th // 2 + 4
        self._trader_hitboxes[tid] = (
            x - half_w, y - half_h,
            x + half_w, y + half_h + 22,
        )

    def _draw_speech(self, x: int, y: int, vote: Dict[str, Any]) -> None:
        action = str(vote.get("action") or "?").upper()
        conf = float(vote.get("confidence") or 0)
        color = {"LONG": "#2E7D32", "SHORT": "#C62828", "WAIT": "#6D4C41"}.get(action, "#455A64")
        label = f"{action} {conf:.0%}"
        self.create_rectangle(
            x - 40, y - 14, x + 40, y + 14,
            fill=PALETTE["paper"], outline=color, width=2, tags="overlay",
        )
        self.create_polygon(
            x - 5, y + 14, x + 5, y + 14, x, y + 22,
            fill=PALETTE["paper"], outline=color, tags="overlay",
        )
        self.create_text(
            x, y, text=label, fill=color,
            font=("Courier New", 9, "bold"), tags="overlay",
        )

    def _draw_overlays_only(self) -> None:
        self.delete("overlay_anim")
        w = max(self.winfo_width(), 320)
        h = max(self.winfo_height(), 280)
        cx, cy, hw, hh = self._monitor_center(w, h)
        glow = "#80CBC4" if self._anim_phase % 8 < 4 else "#26A69A"
        # 屏框呼吸光，提示「这是活行情屏」
        self.create_rectangle(
            cx - hw - 2, cy - hh - 2, cx + hw + 2, cy + hh + 2,
            outline=glow, width=1, tags="overlay_anim",
        )

    def _draw_fallback(self, w: int, h: int) -> None:
        tile = 36
        for row in range(0, h + tile, tile):
            for col in range(0, w + tile, tile):
                shade = "#C9B896" if (row // tile + col // tile) % 2 == 0 else "#B09A78"
                self.create_rectangle(col, row, col + tile, row + tile, fill=shade, outline="#A89070")
        self.create_rectangle(0, 0, w, 36, fill="#5D4037", outline="")
        self.create_text(
            w // 2, 18, text="BNB 交易员议会（素材加载中…）",
            fill="#F5E6C8", font=("Microsoft YaHei UI", 11, "bold"),
        )
        cx, cy = w // 2, int(h * 0.52)
        rw, rh = int(w * 0.28), int(h * 0.16)
        self.create_oval(cx - rw, cy - rh, cx + rw, cy + rh, fill="#8B5A2B", outline="#4A2F1A", width=4)
        n = len(TRADER_DEFS)
        for i, t in enumerate(TRADER_DEFS):
            angle = -math.pi / 2 + i * (2 * math.pi / n)
            x = int(cx + int(w * 0.38) * math.cos(angle))
            y = int(cy + int(h * 0.30) * math.sin(angle))
            self._draw_vector_robot(x, y, t, selected=(t["id"] == self._selected))
            vote = self._votes.get(t["id"])
            if vote:
                self._draw_speech(x, y - 48, vote)

    def _draw_vector_robot(self, x: int, y: int, t: Dict[str, Any], *, selected: bool) -> None:
        body, accent = t["body"], t["accent"]
        self.create_rectangle(x - 16, y - 8, x + 16, y + 14, fill=body, outline="#212121", width=2)
        self.create_rectangle(x - 14, y - 30, x + 14, y - 8, fill="#ECEFF1", outline="#212121", width=2)
        eye = "#FFEB3B" if selected else "#00E676"
        self.create_rectangle(x - 9, y - 24, x - 3, y - 18, fill=eye, outline="")
        self.create_rectangle(x + 3, y - 24, x + 9, y - 18, fill=eye, outline="")
        self.create_oval(x - 3, y - 42, x + 3, y - 36, fill=accent, outline="")
        self.create_text(
            x, y + 34, text=f"{t['emoji']} {t['name']}",
            fill=PALETTE["ink"], font=("Microsoft YaHei UI", 8, "bold"),
        )
        self._trader_hitboxes[t["id"]] = (x - 24, y - 46, x + 24, y + 42)
