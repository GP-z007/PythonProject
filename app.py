from __future__ import annotations

from collections import defaultdict
import faulthandler
import logging
from pathlib import Path, PurePosixPath
from queue import Empty, Queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk
from pygments import lex
from pygments.lexers import get_lexer_for_filename, TextLexer

from analyzer import (
    build_architecture,
    build_folder_view,
    graph_stats,
    project_id,
)
from agent import AgentService, AGENTS
from knowledge import OMLXClient, WeaviateBrain, ProjectIndexer
from sandbox import SandboxManager
from builder import BuilderSandbox, BuilderError
from forge import ForgeService


LOG_DIR = Path(__file__).with_name("logs")
LOG_DIR.mkdir(exist_ok=True)
LOG_PATH = LOG_DIR / "softwareforge.log"
faulthandler.enable()
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
)
LOGGER = logging.getLogger("softwareforge")


ctk.set_appearance_mode("dark")

BG = "#212121"
SIDEBAR = "#171717"
PANEL = "#2f2f2f"
PANEL_2 = "#363636"
PANEL_3 = "#2a2a2a"
SURFACE = PANEL
SURFACE_HOVER = PANEL_2
SURFACE_SOFT = "#282828"
CANVAS_BG = "#141414"
GRID = "#1d1d1d"
BORDER = "#3a3a3a"
TEXT = "#ececec"
MUTED = "#a0a0a0"
TEXT_DIM = "#707070"
SOFT = "#c5c5c5"
UI_FONT = "DejaVu Sans"
CODE_FONT = "DejaVu Sans Mono"
ACCENT = "#ff6d5a"
GREEN = "#31d39b"
RED = "#ff6976"
YELLOW = "#efbd50"

SIDEBAR_EXPANDED_WIDTH = 220
SIDEBAR_COLLAPSED_WIDTH = 60
PAGE_PAD_X = 24
PAGE_PAD_Y = 18
CHAT_MAX_WIDTH = 860
CHAT_SIDE_PADDING = 24
CARD_RADIUS = 14
BUTTON_RADIUS = 9
COMPOSER_RADIUS = 26
NODE_GAP_X = 40
NODE_GAP_Y = 28
GROUP_PAD_X = 30
GROUP_PAD_TOP = 50
GROUP_PAD_BOTTOM = 30

TYPE_COLORS = {
    "security": "#eb6074",
    "frontend": "#4c9aff",
    "service": "#8467ff",
    "worker": "#f59a45",
    "database": "#38b995",
    "infra": "#d5aa3f",
    "external": "#8d97a6",
    "folder": "#e0a64b",
    "file": "#56a6ff",
}

TYPE_LABELS = {
    "security": "Security",
    "frontend": "Frontend",
    "service": "Services",
    "worker": "Workers",
    "database": "Data",
    "infra": "Infrastructure",
    "external": "External",
    "folder": "Folder",
    "file": "File",
}


def darken(hex_color, factor):
    value = hex_color.lstrip("#")
    red = int(value[0:2], 16)
    green = int(value[2:4], 16)
    blue = int(value[4:6], 16)

    return "#{:02x}{:02x}{:02x}".format(
        int(red * factor),
        int(green * factor),
        int(blue * factor),
    )


def rounded_polygon_points(x1, y1, x2, y2, radius):
    radius = min(
        radius,
        abs(x2 - x1) / 2,
        abs(y2 - y1) / 2,
    )

    return [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]


def cubic_bezier_point(points, t):
    x0, y0, x1, y1, x2, y2, x3, y3 = points
    one = 1.0 - t

    x = (
        one ** 3 * x0
        + 3 * one ** 2 * t * x1
        + 3 * one * t ** 2 * x2
        + t ** 3 * x3
    )

    y = (
        one ** 3 * y0
        + 3 * one ** 2 * t * y1
        + 3 * one * t ** 2 * y2
        + t ** 3 * y3
    )

    return (x, y)


class SyntaxCodeView(ctk.CTkFrame):
    COLORS = {
        "Token.Keyword": "#c792ea",
        "Token.Keyword.Type": "#82aaff",
        "Token.Name.Function": "#82aaff",
        "Token.Name.Class": "#ffcb6b",
        "Token.Name.Builtin": "#89ddff",
        "Token.Literal.String": "#c3e88d",
        "Token.Literal.Number": "#f78c6c",
        "Token.Comment": "#65737e",
        "Token.Operator": "#89ddff",
        "Token.Punctuation": "#89a4b6",
        "Token.Name.Tag": "#f07178",
        "Token.Name.Attribute": "#ffcb6b",
        "Token.Generic.Heading": "#82aaff",
    }

    def __init__(self, master):
        ctk.CTkFrame.__init__(
            self,
            master,
            fg_color="#0d1117",
            corner_radius=14,
            border_width=1,
            border_color="#2a3038",
        )

        self._source = ""
        self._path = ""

        top = ctk.CTkFrame(
            self,
            height=38,
            fg_color="#12171e",
            corner_radius=12,
        )
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=5, pady=(5, 0))
        top.grid_propagate(False)

        self.file_label = ctk.CTkLabel(
            top,
            text="No file open",
            text_color="#dfe5ec",
            font=(UI_FONT, 9, "bold"),
        )
        self.file_label.pack(side="left", padx=(12, 8))

        self.language_label = ctk.CTkLabel(
            top,
            text="TEXT",
            height=22,
            corner_radius=8,
            fg_color="#202731",
            text_color="#8fa4bb",
            font=(UI_FONT, 8, "bold"),
        )
        self.language_label.pack(side="left", padx=4)

        self.line_label = ctk.CTkLabel(
            top,
            text="",
            text_color="#657383",
            font=(UI_FONT, 8),
        )
        self.line_label.pack(side="left", padx=5)

        ctk.CTkButton(
            top,
            text="Copy",
            width=52,
            height=24,
            corner_radius=8,
            fg_color="transparent",
            hover_color="#242b34",
            border_width=1,
            border_color="#303844",
            text_color="#aeb8c4",
            font=(UI_FONT, 8),
            command=self._copy_source,
        ).pack(side="right", padx=7)

        self.text = tk.Text(
            self,
            bg="#0d1117",
            fg="#d6dce5",
            insertbackground="#ffffff",
            selectbackground="#31455f",
            relief=tk.FLAT,
            borderwidth=0,
            wrap=tk.NONE,
            font=(CODE_FONT, 12),
            padx=11,
            pady=10,
            spacing1=1,
            spacing3=1,
        )

        yscroll = ctk.CTkScrollbar(
            self,
            orientation="vertical",
            command=self.text.yview,
        )
        xscroll = ctk.CTkScrollbar(
            self,
            orientation="horizontal",
            command=self.text.xview,
        )

        self.text.configure(
            yscrollcommand=yscroll.set,
            xscrollcommand=xscroll.set,
        )

        self.text.grid(row=1, column=0, sticky="nsew", padx=(4, 0), pady=(2, 0))
        yscroll.grid(row=1, column=1, sticky="ns", pady=(2, 0))
        xscroll.grid(row=2, column=0, sticky="ew", padx=(4, 0), pady=(0, 4))

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.text.tag_configure("lineno", foreground="#465261")
        self.text.tag_configure("focusline", background="#26332d")

        for token_name, color in self.COLORS.items():
            self.text.tag_configure(token_name, foreground=color)

    def _token_tag(self, token_type):
        name = str(token_type)

        while name:
            if name in self.COLORS:
                return name

            if "." not in name:
                break

            name = name.rsplit(".", 1)[0]

        return None

    def _copy_source(self):
        if not self._source:
            return

        try:
            self.clipboard_clear()
            self.clipboard_append(self._source)
        except Exception:
            pass

    def show_source(self, path, source, focus_line=None):
        self._source = source
        self._path = path

        language = PurePosixPath(path).suffix.lstrip(".").upper() or "TEXT"
        self.file_label.configure(text=PurePosixPath(path).name)
        self.language_label.configure(text=language[:9])
        self.line_label.configure(text="%d lines" % max(1, len(source.splitlines())))

        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)

        try:
            lexer = get_lexer_for_filename(path, stripnl=False)
        except Exception:
            lexer = TextLexer(stripnl=False)

        lines = source.splitlines(True)

        if not lines:
            lines = [""]

        for line_number, line in enumerate(lines, 1):
            self.text.insert(
                tk.END,
                "%4d  " % line_number,
                ("lineno",),
            )

            for token_type, value in lex(line, lexer):
                tag = self._token_tag(token_type)
                self.text.insert(
                    tk.END,
                    value,
                    (tag,) if tag else (),
                )

        if focus_line:
            widget_line = max(1, int(focus_line))
            self.text.tag_add(
                "focusline",
                "%d.0" % widget_line,
                "%d.end" % widget_line,
            )
            self.text.see("%d.0" % widget_line)

        self.text.configure(state=tk.DISABLED)

    def show_message(self, message):
        self._source = ""
        self._path = ""
        self.file_label.configure(text="Source preview")
        self.language_label.configure(text="TEXT")
        self.line_label.configure(text="")
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", message)
        self.text.configure(state=tk.DISABLED)


class MarkdownBody(tk.Text):
    """Small markdown renderer for agent answers; no raw #/** markers in the UI."""

    INLINE_RE = re.compile(
        r"(`[^`]+`|\*\*\*.+?\*\*\*|\*\*.+?\*\*|__.+?__|\[[0-9]+\]|\*[^*\n]+\*)"
    )

    def __init__(self, master, background, foreground=TEXT, **kwargs):
        tk.Text.__init__(
            self,
            master,
            bg=background,
            fg=foreground,
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=0,
            wrap=tk.WORD,
            font=(UI_FONT, 15),
            padx=0,
            pady=0,
            cursor="arrow",
            **kwargs
        )

        self.tag_configure("body", foreground=foreground, spacing3=4)
        self.tag_configure("h1", foreground="#f6f7f9", font=(UI_FONT, 24, "bold"), spacing1=8, spacing3=6)
        self.tag_configure("h2", foreground="#f3f4f6", font=(UI_FONT, 20, "bold"), spacing1=7, spacing3=4)
        self.tag_configure("h3", foreground="#e8ebef", font=(UI_FONT, 17, "bold"), spacing1=6, spacing3=3)
        self.tag_configure("bold", foreground="#f5f7fa", font=(UI_FONT, 15, "bold"))
        self.tag_configure("italic", foreground="#cbd2db", font=(UI_FONT, 15, "italic"))
        self.tag_configure("inline_code", foreground="#ffb4a9", background="#252a31", font=(CODE_FONT, 12))
        self.tag_configure("code", foreground="#cbd5e1", background="#0d1117", font=(CODE_FONT, 12), lmargin1=12, lmargin2=12, spacing1=1, spacing3=1)
        self.tag_configure("code_lang", foreground="#77879a", background="#0d1117", font=(UI_FONT, 8, "bold"), lmargin1=12, spacing1=6)
        self.tag_configure("bullet", foreground=foreground, lmargin1=8, lmargin2=24, spacing3=3)
        self.tag_configure("number", foreground=foreground, lmargin1=8, lmargin2=26, spacing3=3)
        self.tag_configure("quote", foreground="#aeb8c5", lmargin1=12, lmargin2=20, spacing1=3, spacing3=3)
        self.tag_configure("citation", foreground=ACCENT, font=(UI_FONT, 11, "bold"))
        self.tag_configure("table", foreground="#cbd5e1", font=(CODE_FONT, 10), spacing3=2)
        self.tag_configure("rule", foreground="#414a56")

    def _insert_inline(self, text, base_tag="body"):
        cursor = 0

        for match in self.INLINE_RE.finditer(text):
            if match.start() > cursor:
                self.insert(tk.END, text[cursor:match.start()], (base_tag,))

            token = match.group(0)

            if token.startswith("***") and token.endswith("***"):
                self._insert_emphasis_content(token[3:-3], base_tag, ("bold", "italic"))
            elif token.startswith("**") and token.endswith("**"):
                self._insert_emphasis_content(token[2:-2], base_tag, ("bold",))
            elif token.startswith("__") and token.endswith("__"):
                self._insert_emphasis_content(token[2:-2], base_tag, ("bold",))
            elif token.startswith("`") and token.endswith("`"):
                self.insert(tk.END, token[1:-1], ("inline_code",))
            elif re.fullmatch(r"\[[0-9]+\]", token):
                self.insert(tk.END, token, ("citation",))
            elif token.startswith("*") and token.endswith("*"):
                self.insert(tk.END, token[1:-1], (base_tag, "italic"))
            else:
                self.insert(tk.END, token, (base_tag,))

            cursor = match.end()

        if cursor < len(text):
            self.insert(tk.END, text[cursor:], (base_tag,))

    def _insert_emphasis_content(self, text, base_tag, emphasis):
        parts = re.split(r"(`[^`]+`)", text)
        for part in parts:
            if not part:
                continue
            if part.startswith("`") and part.endswith("`"):
                self.insert(tk.END, part[1:-1], ("inline_code",))
            else:
                self.insert(tk.END, part, (base_tag,) + emphasis)

    def render(self, markdown):
        self.configure(state=tk.NORMAL)
        self.delete("1.0", tk.END)

        lines = (markdown or "").splitlines()
        in_code = False
        code_language = ""
        estimated_lines = 1

        for line in lines:
            stripped = line.strip()

            if stripped.startswith("```"):
                if not in_code:
                    in_code = True
                    code_language = stripped[3:].strip()
                    if code_language:
                        self.insert(tk.END, code_language.upper() + "\n", ("code_lang",))
                        estimated_lines += 1
                else:
                    in_code = False
                    self.insert(tk.END, "\n", ("body",))
                    estimated_lines += 1
                continue

            if in_code:
                self.insert(tk.END, line + "\n", ("code",))
                estimated_lines += 1 + max(0, len(line) // 92)
                continue

            heading = re.match(r"^(#{1,3})\s+(.*)$", line)
            if heading:
                level = len(heading.group(1))
                self._insert_inline(heading.group(2), "h%d" % level)
                self.insert(tk.END, "\n", ("h%d" % level,))
                estimated_lines += 2
                continue

            if re.match(r"^\s*[-*_]{3,}\s*$", line):
                self.insert(tk.END, "────────────────────────\n", ("rule",))
                estimated_lines += 1
                continue

            bullet = re.match(r"^\s*[-+*]\s+(.*)$", line)
            if bullet:
                self.insert(tk.END, "• ", ("bullet",))
                self._insert_inline(bullet.group(1), "bullet")
                self.insert(tk.END, "\n", ("bullet",))
                estimated_lines += 1 + len(bullet.group(1)) // 88
                continue

            numbered = re.match(r"^\s*(\d+)\.\s+(.*)$", line)
            if numbered:
                self.insert(tk.END, numbered.group(1) + ". ", ("number",))
                self._insert_inline(numbered.group(2), "number")
                self.insert(tk.END, "\n", ("number",))
                estimated_lines += 1 + len(numbered.group(2)) // 88
                continue

            quote = re.match(r"^\s*>\s?(.*)$", line)
            if quote:
                self.insert(tk.END, "│ ", ("quote",))
                self._insert_inline(quote.group(1), "quote")
                self.insert(tk.END, "\n", ("quote",))
                estimated_lines += 1 + len(quote.group(1)) // 88
                continue

            if stripped.startswith("|") and stripped.endswith("|"):
                cells = [cell.strip() for cell in stripped.strip("|").split("|")]
                if cells and all(re.fullmatch(r":?-{2,}:?", cell or "--") for cell in cells):
                    continue
                self.insert(tk.END, "   ·   ".join(cells) + "\n", ("table",))
                estimated_lines += 1
                continue

            if not stripped:
                self.insert(tk.END, "\n", ("body",))
                estimated_lines += 1
                continue

            self._insert_inline(line, "body")
            self.insert(tk.END, "\n", ("body",))
            estimated_lines += 1 + len(line) // 92

        # Text.count(displaylines) is unreliable before a message has been laid out;
        # on Tk/X11 it could report dozens of wrapped lines for a one-line message
        # and create the giant empty chat bubbles seen in the UI.  The renderer
        # already tracks a conservative wrap estimate, so size from that instead.
        self.configure(height=max(2, min(180, estimated_lines)))
        self.configure(state=tk.DISABLED)


class ChatMessage(ctk.CTkFrame):
    def __init__(
        self,
        master,
        role,
        content,
        agent_name="Assistant",
        meta="",
        sources=None,
        on_source=None,
        on_more_sources=None,
    ):
        is_user = role == "user"

        ctk.CTkFrame.__init__(
            self,
            master,
            fg_color="transparent",
            corner_radius=0,
        )

        row = ctk.CTkFrame(
            self,
            fg_color="transparent",
            corner_radius=0,
        )
        row.pack(fill="x")

        if is_user:
            # User messages do not need a full Markdown Text widget.  A label sizes
            # naturally to its content and prevents one-line questions from turning
            # into several-hundred-pixel-tall empty cards on Tk/X11.
            bubble = ctk.CTkFrame(
                row,
                fg_color="#2f2f2f",
                corner_radius=18,
            )
            bubble.pack(side="right", padx=(160, 6), pady=(8, 6))

            ctk.CTkLabel(
                bubble,
                text=content,
                text_color="#f2f2f2",
                font=(UI_FONT, 15),
                justify="left",
                anchor="w",
                wraplength=620,
            ).pack(padx=16, pady=11)
        else:
            bubble = ctk.CTkFrame(
                row,
                fg_color="transparent",
                corner_radius=0,
            )
            bubble.pack(fill="x", expand=True, padx=(6, 24), pady=(6, 10))

            header = ctk.CTkFrame(
                bubble,
                fg_color="transparent",
            )
            header.pack(fill="x", padx=14, pady=(10, 2))

            ctk.CTkLabel(
                header,
                text="✦ %s" % agent_name,
                text_color="#c8c8c8",
                font=(UI_FONT, 10, "bold"),
            ).pack(side="left")

            body = MarkdownBody(
                bubble,
                background=BG,
                foreground="#dedede",
                width=78,
            )
            body.pack(fill="x", padx=14, pady=(2, 12))
            body.render(content)

            if sources and on_source:
                citations = ctk.CTkFrame(bubble, fg_color="transparent")
                citations.pack(fill="x", padx=14, pady=(0, 10))
                ctk.CTkLabel(
                    citations,
                    text="Sources",
                    text_color=MUTED,
                    font=(UI_FONT, 8, "bold"),
                ).pack(side="left", padx=(0, 7))
                for index, source in enumerate(sources[:2], 1):
                    path = source.get("path", "")
                    start_line = source.get("start_line", 1)
                    end_line = source.get("end_line", start_line)
                    ctk.CTkButton(
                        citations,
                        text="[%d] %s · L%s–%s" % (
                            index,
                            PurePosixPath(path).name,
                            start_line,
                            end_line,
                        ),
                        width=120,
                        height=25,
                        corner_radius=8,
                        fg_color=PANEL,
                        hover_color=PANEL_2,
                        text_color=SOFT,
                        font=(UI_FONT, 8),
                        command=lambda p=path, l=start_line: on_source(p, l),
                    ).pack(side="left", padx=(0, 4))
                if len(sources) > 2:
                    ctk.CTkButton(
                        citations,
                        text="+%d more" % (len(sources) - 2),
                        width=88,
                        height=25,
                        corner_radius=8,
                        fg_color="transparent",
                        hover_color=PANEL_2,
                        text_color=MUTED,
                        font=(UI_FONT, 8),
                        command=on_more_sources,
                    ).pack(side="left", padx=(0, 4))

            if meta:
                ctk.CTkLabel(
                    bubble,
                    text=meta,
                    text_color=TEXT_DIM,
                    font=(UI_FONT, 8),
                ).pack(anchor="w", padx=14, pady=(0, 8))


class WorkflowCanvas(tk.Canvas):
    NODE_W = 224
    NODE_H = 88

    def __init__(
        self,
        master,
        view,
        on_select=None,
        on_open=None,
        initial_positions=None,
        initial_group_boxes=None,
        show_groups=True,
        **kwargs
    ):
        tk.Canvas.__init__(
            self,
            master,
            bg=CANVAS_BG,
            highlightthickness=0,
            **kwargs
        )

        self.view = view or {
            "components": [],
            "connections": [],
        }

        self.on_select = on_select
        self.on_open = on_open
        self.show_groups = show_groups

        self.node_positions = dict(
            initial_positions
            or {}
        )

        self.group_boxes = dict(
            initial_group_boxes
            or {}
        )

        self.node_items = {}
        self.group_items = {}
        self.group_nodes = defaultdict(list)
        self.edge_items = {}

        self.selected = None
        self.drag_node = None
        self.drag_group = None
        self.drag_last = None

        self.zoom = 1.0

        self.flow_running = False
        self.flow_edges = []
        self.flow_edge_index = 0
        self.flow_t = 0.0
        self.flow_dots = []
        self.flow_active_edge_id = None

        self.bind("<ButtonPress-2>", self._pan_start)
        self.bind("<B2-Motion>", self._pan_move)
        self.bind("<ButtonPress-3>", self._pan_start)
        self.bind("<B3-Motion>", self._pan_move)
        self.bind("<MouseWheel>", self._mousewheel)

        self.bind(
            "<Button-4>",
            lambda event: self._zoom_at(
                event.x,
                event.y,
                1.08,
            ),
        )

        self.bind(
            "<Button-5>",
            lambda event: self._zoom_at(
                event.x,
                event.y,
                0.92,
            ),
        )

        self.bind(
            "<Escape>",
            lambda event: self.clear_focus(),
        )

        self.focus_set()
        self._draw_after = self.after(50, self.draw)

    def destroy(self):
        try:
            self.after_cancel(self._draw_after)
        except (AttributeError, tk.TclError):
            pass
        tk.Canvas.destroy(self)

    def components(self):
        return self.view.get("components", [])

    def edges(self):
        return self.view.get("connections", [])

    def _pan_start(self, event):
        self.scan_mark(event.x, event.y)

    def _pan_move(self, event):
        self.scan_dragto(
            event.x,
            event.y,
            gain=1,
        )

    def _mousewheel(self, event):
        self._zoom_at(
            event.x,
            event.y,
            1.08 if event.delta > 0 else 0.92,
        )

    def _zoom_at(self, x, y, factor):
        new_zoom = self.zoom * factor

        if not 0.56 <= new_zoom <= 1.75:
            return

        world_x = self.canvasx(x)
        world_y = self.canvasy(y)

        self.scale(
            "world",
            world_x,
            world_y,
            factor,
            factor,
        )

        self._scale_model(
            world_x,
            world_y,
            factor,
        )

        self.zoom = new_zoom

    def _scale_model(self, pivot_x, pivot_y, factor):
        for node_id, position in list(
            self.node_positions.items()
        ):
            x, y = position

            self.node_positions[node_id] = (
                pivot_x + (x - pivot_x) * factor,
                pivot_y + (y - pivot_y) * factor,
            )

        for group_type, bounds in list(
            self.group_boxes.items()
        ):
            if not bounds:
                continue

            left, top, right, bottom = bounds

            self.group_boxes[group_type] = (
                pivot_x + (left - pivot_x) * factor,
                pivot_y + (top - pivot_y) * factor,
                pivot_x + (right - pivot_x) * factor,
                pivot_y + (bottom - pivot_y) * factor,
            )

    def draw_grid(self):
        self.delete("grid")

        for x in range(0, 2000, 28):
            self.create_line(
                x,
                0,
                x,
                1300,
                fill=GRID,
                tags=("grid",),
            )

        for y in range(0, 1300, 28):
            self.create_line(
                0,
                y,
                2000,
                y,
                fill=GRID,
                tags=("grid",),
            )

        self.tag_lower("grid")

    def _layout(self):
        components = self.components()

        if not self.show_groups:
            result = {}
            columns = 3

            for index, component in enumerate(components):
                column = index % columns
                row = index // columns

                result[component["id"]] = (
                    260 + column * (self.NODE_W + NODE_GAP_X + 50),
                    210 + row * (self.NODE_H + NODE_GAP_Y + 25),
                )

            return result

        groups = defaultdict(list)

        for component in components:
            groups[
                component.get("type", "service")
            ].append(
                component["id"]
            )

        origins = {
            "frontend": (220, 280),
            "security": (220, 690),
            "service": (660, 270),
            "worker": (1090, 250),
            "database": (660, 760),
            "infra": (1100, 710),
            "external": (1450, 420),
        }

        result = {}

        for group_type, node_ids in groups.items():
            origin = origins.get(
                group_type,
                (680, 320),
            )

            columns = (
                2
                if len(node_ids) >= 3
                else 1
            )

            for index, node_id in enumerate(node_ids):
                result[node_id] = (
                    origin[0] + (index % columns) * (self.NODE_W + NODE_GAP_X),
                    origin[1] + (index // columns) * (self.NODE_H + NODE_GAP_Y),
                )

        return result

    def _derive_group_box(self, group_type):
        node_ids = self.group_nodes.get(
            group_type,
            [],
        )

        if not node_ids:
            return None

        xs = [
            self.node_positions[node_id][0]
            for node_id in node_ids
        ]

        ys = [
            self.node_positions[node_id][1]
            for node_id in node_ids
        ]

        return (
            min(xs) - self.NODE_W / 2 - GROUP_PAD_X,
            min(ys) - self.NODE_H / 2 - GROUP_PAD_TOP,
            max(xs) + self.NODE_W / 2 + GROUP_PAD_X,
            max(ys) + self.NODE_H / 2 + GROUP_PAD_BOTTOM,
        )

    def draw(self):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return

        if not self.components():
            self.draw_grid()

            self.create_text(
                400,
                250,
                text="No analyzed files here\n\nThis folder contains no supported source files.",
                fill=MUTED,
                font=(UI_FONT, 12),
                justify="center",
                tags=("world",),
            )
            return

        saved_positions = dict(
            self.node_positions
        )

        saved_boxes = dict(
            self.group_boxes
        )

        self.delete("all")

        self.node_items = {}
        self.group_items = {}
        self.group_nodes = defaultdict(list)
        self.edge_items = {}

        self.draw_grid()

        defaults = self._layout()
        self.node_positions = {}

        for component in self.components():
            node_id = component["id"]

            self.node_positions[node_id] = saved_positions.get(
                node_id,
                defaults.get(
                    node_id,
                    (700, 400),
                ),
            )

            if self.show_groups:
                self.group_nodes[
                    component.get(
                        "type",
                        "service",
                    )
                ].append(node_id)

        self.group_boxes = {}

        if self.show_groups:
            for group_type in self.group_nodes:
                self.group_boxes[group_type] = saved_boxes.get(
                    group_type,
                    self._derive_group_box(group_type),
                )

            self._draw_groups()

        self._draw_edges()

        for component in self.components():
            self._draw_node(component)

        self.tag_raise("node")
        self.tag_raise("node_text")
        self.tag_raise("port")

        self.configure(
            scrollregion=(0, 0, 1900, 1250)
        )

    def _draw_groups(self):
        for group_type, bounds in self.group_boxes.items():
            if not bounds:
                continue

            left, top, right, bottom = bounds
            color = TYPE_COLORS.get(
                group_type,
                "#8d97a6",
            )

            group_tag = "group::%s" % group_type

            body = self.create_polygon(
                rounded_polygon_points(
                    left,
                    top,
                    right,
                    bottom,
                    18,
                ),
                smooth=True,
                splinesteps=36,
                fill=darken(color, 0.11),
                outline=darken(color, 0.70),
                width=1,
                tags=("world", "group", group_tag),
            )

            title = self.create_text(
                left + 17,
                top + 18,
                text=TYPE_LABELS.get(
                    group_type,
                    group_type.title(),
                ).upper(),
                anchor="w",
                fill=color,
                font=(UI_FONT, 9, "bold"),
                tags=("world", "group_title", group_tag),
            )

            handle = self.create_text(
                right - 18,
                top + 18,
                text="⋮⋮",
                anchor="e",
                fill=darken(color, 0.86),
                font=(UI_FONT, 11, "bold"),
                tags=("world", "group_title", group_tag),
            )

            self.group_items[group_type] = (
                body,
                title,
                handle,
            )

            self.tag_bind(
                group_tag,
                "<ButtonPress-1>",
                lambda event, gt=group_type:
                    self._group_press(event, gt),
            )

            self.tag_bind(
                group_tag,
                "<B1-Motion>",
                lambda event, gt=group_type:
                    self._group_drag(event, gt),
            )

            self.tag_bind(
                group_tag,
                "<ButtonRelease-1>",
                lambda event, gt=group_type:
                    self._group_release(event, gt),
            )
            self.tag_bind(
                group_tag,
                "<Button-3>",
                lambda event, gt=group_type:
                    self._group_menu(event, gt),
            )

    def _group_menu(self, event, group_type):
        menu = tk.Menu(self, tearoff=False, bg=SURFACE, fg=TEXT)
        menu.add_command(
            label="Fit group",
            command=lambda: self._fit_group(group_type),
        )
        menu.tk_popup(self.winfo_rootx() + event.x, self.winfo_rooty() + event.y)

    def _fit_group(self, group_type):
        bounds = self._derive_group_box(group_type)
        if bounds:
            self.group_boxes[group_type] = bounds
            self.draw()

    def _draw_edges(self):
        for edge in self.edges():
            route = self._edge_route(
                edge["from"],
                edge["to"],
            )

            line = self.create_line(
                *route,
                smooth=True,
                splinesteps=48,
                fill="#4b5562",
                width=2,
                arrow=(
                    tk.BOTH
                    if edge.get("bidirectional")
                    else tk.LAST
                ),
                arrowshape=(8, 9, 4),
                tags=("world", "edge"),
            )

            mid_x, mid_y = cubic_bezier_point(
                route,
                0.5,
            )

            label = self.create_text(
                mid_x,
                mid_y - 11,
                text="%s ×%s" % (
                    edge.get("relation", "uses"),
                    edge.get("count", 1),
                ),
                fill="#8c98a6",
                font=(UI_FONT, 7, "bold"),
                state=tk.HIDDEN,
                tags=("world", "edge_label"),
            )

            self.edge_items[edge["id"]] = {
                "line": line,
                "label": label,
                "edge": edge,
            }

    def _draw_node(self, component):
        node_id = component["id"]
        x, y = self.node_positions[node_id]

        kind = component.get(
            "node_kind",
            "module",
        )

        color = TYPE_COLORS.get(
            component.get(
                "type",
                "service",
            ),
            "#8d97a6",
        )

        if kind == "folder":
            color = TYPE_COLORS["folder"]
        elif kind == "file":
            color = TYPE_COLORS["file"]

        node_tag = "node::%s" % node_id

        body = self.create_polygon(
            rounded_polygon_points(
                x - self.NODE_W / 2,
                y - self.NODE_H / 2,
                x + self.NODE_W / 2,
                y + self.NODE_H / 2,
                13,
            ),
            smooth=True,
            splinesteps=36,
            fill="#202124",
            outline="#34363a",
            width=1,
            tags=("world", "node", node_tag),
        )

        self.create_rectangle(
            x - self.NODE_W / 2,
            y - self.NODE_H / 2 + 12,
            x - self.NODE_W / 2 + 3,
            y + self.NODE_H / 2 - 12,
            fill=color,
            outline="",
            tags=("world", "node", node_tag),
        )

        icon = {
            "folder": "▣",
            "file": "≡",
            "module": component.get(
                "type",
                "service",
            )[0].upper(),
        }.get(kind, "•")

        self.create_text(
            x - self.NODE_W / 2 + 27,
            y,
            text=icon,
            fill=color,
            font=(UI_FONT, 12, "bold"),
            tags=("world", "node_text", node_tag),
        )

        self.create_text(
            x - self.NODE_W / 2 + 50,
            y - 11,
            text=component.get(
                "label",
                node_id,
            )[:28],
            anchor="w",
            fill=TEXT,
            font=(UI_FONT, 11, "bold"),
            tags=("world", "node_text", node_tag),
        )

        self.create_text(
            x - self.NODE_W / 2 + 50,
            y + 12,
            text=component.get(
                "subtitle",
                "",
            )[:34],
            anchor="w",
            fill=MUTED,
            font=(UI_FONT, 8),
            tags=("world", "node_text", node_tag),
        )

        if component.get("folder_path"):
            self.create_text(
                x + self.NODE_W / 2 - 14,
                y - self.NODE_H / 2 + 14,
                text="↗",
                fill=color,
                font=(UI_FONT, 11, "bold"),
                tags=("world", "node_text", node_tag),
            )

        items = self.find_withtag(node_tag)
        self.node_items[node_id] = items

        self.tag_bind(
            node_tag,
            "<ButtonPress-1>",
            lambda event, nid=node_id:
                self._node_press(event, nid),
        )

        self.tag_bind(
            node_tag,
            "<B1-Motion>",
            lambda event, nid=node_id:
                self._node_drag(event, nid),
        )

        self.tag_bind(
            node_tag,
            "<ButtonRelease-1>",
            lambda event, nid=node_id:
                self._node_release(event, nid),
        )

        self.tag_bind(
            node_tag,
            "<Double-Button-1>",
            lambda event, item=component:
                self._open_component(item),
        )

        self.tag_bind(
            node_tag,
            "<Enter>",
            lambda event, nid=node_id:
                self._node_hover(nid, True),
        )

        self.tag_bind(
            node_tag,
            "<Leave>",
            lambda event, nid=node_id:
                self._node_hover(nid, False),
        )

    def _node_hover(self, node_id, entering):
        self.configure(cursor="hand2" if entering else "")

        if self.selected == node_id:
            return

        items = self.node_items.get(node_id)

        if not items:
            return

        try:
            self.itemconfigure(
                items[0],
                outline=("#596575" if entering else "#3a414b"),
                width=(2 if entering else 1),
                fill=("#282a2e" if entering else "#202124"),
            )
        except Exception:
            pass

    def _open_component(self, component):
        if self.on_open:
            self.on_open(component)

    def _edge_route(self, source_id, target_id):
        sx0, sy0 = self.node_positions[source_id]
        tx0, ty0 = self.node_positions[target_id]

        # Tk scales the visible cards. Routing must use the same effective card
        # size or edges drift after Fit/zoom and then snap when a node is moved.
        node_w = self.NODE_W * self.zoom
        node_h = self.NODE_H * self.zoom

        dx = tx0 - sx0
        dy = ty0 - sy0

        if abs(dy) > abs(dx) * 0.78:
            if dy >= 0:
                sx = sx0
                sy = sy0 + node_h / 2
                tx = tx0
                ty = ty0 - node_h / 2
                direction = 1
            else:
                sx = sx0
                sy = sy0 - node_h / 2
                tx = tx0
                ty = ty0 + node_h / 2
                direction = -1

            curve = max(
                58 * self.zoom,
                abs(ty - sy) * 0.42,
            )

            return [
                sx,
                sy,
                sx,
                sy + direction * curve,
                tx,
                ty - direction * curve,
                tx,
                ty,
            ]

        if dx >= 0:
            sx = sx0 + node_w / 2
            tx = tx0 - node_w / 2
            direction = 1
        else:
            sx = sx0 - node_w / 2
            tx = tx0 + node_w / 2
            direction = -1

        sy = sy0
        ty = ty0

        curve = max(
            58 * self.zoom,
            abs(tx - sx) * 0.40,
        )

        return [
            sx,
            sy,
            sx + direction * curve,
            sy,
            tx - direction * curve,
            ty,
            tx,
            ty,
        ]

    def _node_press(self, event, node_id):
        self.focus_set()
        self.drag_node = node_id
        self.drag_group = None

        self.drag_last = (
            self.canvasx(event.x),
            self.canvasy(event.y),
        )

        self.select(node_id)

    def _node_drag(self, event, node_id):
        if self.drag_node != node_id:
            return

        current = (
            self.canvasx(event.x),
            self.canvasy(event.y),
        )

        dx = current[0] - self.drag_last[0]
        dy = current[1] - self.drag_last[1]
        self.drag_last = current

        self._move_node(
            node_id,
            dx,
            dy,
        )

        self._refresh_edges()

    def _node_release(self, event, node_id):
        self.drag_node = None
        self.drag_last = None

    def _group_press(self, event, group_type):
        self.focus_set()
        self.drag_group = group_type
        self.drag_node = None

        self.drag_last = (
            self.canvasx(event.x),
            self.canvasy(event.y),
        )

    def _group_drag(self, event, group_type):
        if self.drag_group != group_type:
            return

        current = (
            self.canvasx(event.x),
            self.canvasy(event.y),
        )

        dx = current[0] - self.drag_last[0]
        dy = current[1] - self.drag_last[1]
        self.drag_last = current

        for node_id in self.group_nodes.get(
            group_type,
            [],
        ):
            self._move_node(
                node_id,
                dx,
                dy,
            )

        for item in self.group_items.get(
            group_type,
            (),
        ):
            self.move(
                item,
                dx,
                dy,
            )

        left, top, right, bottom = self.group_boxes[group_type]

        self.group_boxes[group_type] = (
            left + dx,
            top + dy,
            right + dx,
            bottom + dy,
        )

        self._refresh_edges()

    def _group_release(self, event, group_type):
        self.drag_group = None
        self.drag_last = None

    def _move_node(self, node_id, dx, dy):
        x, y = self.node_positions[node_id]

        self.node_positions[node_id] = (
            x + dx,
            y + dy,
        )

        for item in self.node_items[node_id]:
            self.move(
                item,
                dx,
                dy,
            )

    def _refresh_edges(self):
        for item in self.edge_items.values():
            edge = item["edge"]
            route = self._edge_route(
                edge["from"],
                edge["to"],
            )

            self.coords(
                item["line"],
                *route,
            )

            mid_x, mid_y = cubic_bezier_point(
                route,
                0.5,
            )

            if item.get("label"):
                self.coords(
                    item["label"],
                    mid_x,
                    mid_y - 11 * self.zoom,
                )

    def select(self, node_id):
        self.selected = node_id
        neighbors = {node_id}
        connected_edges = []

        for item in self.edge_items.values():
            edge = item["edge"]

            active = (
                edge["from"] == node_id
                or edge["to"] == node_id
            )

            if active:
                neighbors.add(
                    edge["to"]
                    if edge["from"] == node_id
                    else edge["from"]
                )
                connected_edges.append(edge)

            self.itemconfigure(
                item["line"],
                fill=(
                    ACCENT
                    if active
                    else "#343a43"
                ),
                width=(
                    3
                    if active
                    else 1
                ),
            )

            if item.get("label"):
                self.itemconfigure(
                    item["label"],
                    state=(tk.NORMAL if active else tk.HIDDEN),
                    fill=("#f4a397" if active else "#66717f"),
                )

        for candidate, items in self.node_items.items():
            body = items[0]

            if candidate == node_id:
                self.itemconfigure(
                    body,
                    outline=ACCENT,
                    width=3,
                    fill="#242424",
                )
            elif candidate in neighbors:
                self.itemconfigure(
                    body,
                    outline="#69717d",
                    width=2,
                    fill="#202124",
                )
            else:
                self.itemconfigure(
                    body,
                    outline="#272c33",
                    width=1,
                    fill="#18191c",
                )

        component = next(
            (
                item
                for item in self.components()
                if item["id"] == node_id
            ),
            None,
        )

        if component is not None and self.on_select:
            self.on_select(
                component,
                connected_edges,
            )

    def clear_focus(self):
        self.selected = None

        for items in self.node_items.values():
            self.itemconfigure(
                items[0],
                outline="#3a414b",
                width=1,
                fill="#202124",
            )

        for item in self.edge_items.values():
            self.itemconfigure(
                item["line"],
                fill="#4b5562",
                width=2,
            )

            if item.get("label"):
                self.itemconfigure(
                    item["label"],
                    state=tk.HIDDEN,
                )

    def auto_arrange(self):
        self.stop_flow()

        # Arrange is a logical-layout operation. Reset the visual zoom first so
        # repeated Fit → Arrange cycles cannot accumulate transformed card sizes.
        if abs(self.zoom - 1.0) > 0.01:
            self.zoom = 1.0
            self.draw()

        targets = self._layout()
        starts = dict(self.node_positions)
        steps = 22

        def frame(index):
            progress = index / float(steps)
            eased = 1 - (1 - progress) ** 3

            for node_id, target in targets.items():
                start = starts.get(
                    node_id,
                    target,
                )

                self.node_positions[node_id] = (
                    start[0]
                    + (target[0] - start[0]) * eased,
                    start[1]
                    + (target[1] - start[1]) * eased,
                )

            self._apply_node_positions()
            self._refresh_edges()

            if index < steps:
                self.after(
                    16,
                    lambda:
                        frame(index + 1),
                )
            else:
                if self.show_groups:
                    self.group_boxes = {
                        group_type:
                            self._derive_group_box(group_type)
                        for group_type in self.group_nodes
                    }

                self.draw()

        frame(0)

    def _apply_node_positions(self):
        for node_id, items in self.node_items.items():
            bbox = self.bbox(items[0])

            if not bbox:
                continue

            current_x = (bbox[0] + bbox[2]) / 2
            current_y = (bbox[1] + bbox[3]) / 2
            target = self.node_positions[node_id]

            dx = target[0] - current_x
            dy = target[1] - current_y

            for item in items:
                self.move(item, dx, dy)

    def fit_view(self):
        self.update_idletasks()
        bbox = self.bbox("world")

        if not bbox:
            return

        content_w = max(1, bbox[2] - bbox[0])
        content_h = max(1, bbox[3] - bbox[1])
        view_w = max(1, self.winfo_width() - 96)
        view_h = max(1, self.winfo_height() - 96)

        factor = min(
            view_w / content_w,
            view_h / content_h,
        )

        factor = max(
            0.68,
            min(1.22, factor),
        )

        center_x = (bbox[0] + bbox[2]) / 2
        center_y = (bbox[1] + bbox[3]) / 2

        if abs(factor - 1.0) > 0.01:
            self.scale(
                "world",
                center_x,
                center_y,
                factor,
                factor,
            )

            self._scale_model(
                center_x,
                center_y,
                factor,
            )

            self.zoom *= factor

        bbox = self.bbox("world")

        if not bbox:
            return

        margin = 90
        left = bbox[0] - margin
        top = bbox[1] - margin
        right = bbox[2] + margin
        bottom = bbox[3] + margin

        self.configure(
            scrollregion=(left, top, right, bottom)
        )

        self.update_idletasks()

        region_w = max(1, right - left)
        region_h = max(1, bottom - top)
        visible_w = max(1, self.winfo_width())
        visible_h = max(1, self.winfo_height())
        center_x = (bbox[0] + bbox[2]) / 2
        center_y = (bbox[1] + bbox[3]) / 2

        if region_w > visible_w:
            fraction = (
                center_x - visible_w / 2 - left
            ) / max(1, region_w - visible_w)
            self.xview_moveto(max(0.0, min(1.0, fraction)))
        else:
            self.xview_moveto(0.0)

        if region_h > visible_h:
            fraction = (
                center_y - visible_h / 2 - top
            ) / max(1, region_h - visible_h)
            self.yview_moveto(max(0.0, min(1.0, fraction)))
        else:
            self.yview_moveto(0.0)

    def focus_search(self, query):
        query = query.strip().lower()

        if not query:
            return None

        matches = [
            component
            for component in self.components()
            if (
                query in component.get(
                    "label",
                    "",
                ).lower()
                or query in component.get(
                    "raw_label",
                    "",
                ).lower()
                or query in component.get(
                    "file_path",
                    "",
                ).lower()
            )
        ]

        if not matches:
            return None

        component = matches[0]
        self.select(component["id"])
        return component

    def _restore_edge_style(self, item):
        edge = item["edge"]

        selected_active = (
            self.selected
            and (
                edge["from"] == self.selected
                or edge["to"] == self.selected
            )
        )

        self.itemconfigure(
            item["line"],
            fill=(
                ACCENT
                if selected_active
                else "#4b5562"
            ),
            width=(
                3
                if selected_active
                else 2
            ),
        )

        if item.get("label"):
            self.itemconfigure(
                item["label"],
                state=(tk.NORMAL if selected_active else tk.HIDDEN),
                fill=("#f4a397" if selected_active else "#66717f"),
            )

    def toggle_flow(self):
        if self.flow_running:
            self.stop_flow()
            return False

        if self.selected:
            flow_edges = [
                item
                for item in self.edge_items.values()
                if (
                    item["edge"]["from"] == self.selected
                    or item["edge"]["to"] == self.selected
                )
            ]
        else:
            flow_edges = list(
                self.edge_items.values()
            )

        flow_edges.sort(
            key=lambda item: (
                self.node_positions[
                    item["edge"]["from"]
                ][0],
                self.node_positions[
                    item["edge"]["from"]
                ][1],
                -item["edge"].get("count", 1),
            )
        )

        if not flow_edges:
            return False

        self.flow_running = True
        self.flow_edges = flow_edges
        self.flow_edge_index = 0
        self.flow_t = 0.0
        self.flow_active_edge_id = None

        self.flow_dots = []

        for color, radius in (
            ("#ffffff", 4.2),
            ("#ffd2cc", 3.5),
            ("#ff9a8d", 2.8),
            (ACCENT, 2.2),
        ):
            dot = self.create_oval(
                -20,
                -20,
                -10,
                -10,
                fill=color,
                outline="",
                tags=("world", "flow_dot"),
            )

            self.flow_dots.append(
                (dot, radius)
            )

        self.tag_raise("flow_dot")
        self._flow_tick()
        return True

    def stop_flow(self):
        self.flow_running = False

        if self.flow_active_edge_id:
            active = self.edge_items.get(
                self.flow_active_edge_id
            )

            if active:
                self._restore_edge_style(active)

        for dot, _ in self.flow_dots:
            try:
                self.delete(dot)
            except Exception:
                pass

        self.flow_dots = []
        self.flow_active_edge_id = None

    def _flow_tick(self):
        if (
            not self.flow_running
            or not self.winfo_exists()
            or not self.flow_edges
        ):
            return

        current = self.flow_edges[
            self.flow_edge_index
        ]

        edge = current["edge"]
        edge_id = edge["id"]

        if self.flow_active_edge_id != edge_id:
            if self.flow_active_edge_id:
                previous = self.edge_items.get(
                    self.flow_active_edge_id
                )

                if previous:
                    self._restore_edge_style(previous)

            self.flow_active_edge_id = edge_id

            self.itemconfigure(
                current["line"],
                fill=ACCENT,
                width=4,
            )

        # Flow uses the ACTUAL visible connector, so Fit/zoom cannot desync it.
        points = self.coords(
            current["line"]
        )

        if len(points) != 8:
            self.after(
                30,
                self._flow_tick,
            )
            return

        for index, (dot, radius) in enumerate(
            self.flow_dots
        ):
            t = max(
                0.0,
                self.flow_t
                - index * 0.035,
            )

            x, y = cubic_bezier_point(
                points,
                t,
            )

            self.coords(
                dot,
                x - radius,
                y - radius,
                x + radius,
                y + radius,
            )

        self.flow_t += 0.028

        if self.flow_t >= 1.06:
            self._restore_edge_style(current)
            self.flow_t = 0.0

            self.flow_edge_index = (
                self.flow_edge_index + 1
            ) % len(self.flow_edges)

        self.after(
            30,
            self._flow_tick,
        )



class SoftwareForge(ctk.CTk):
    NAV = ("Project", "Agent", "Forge", "Canvas")

    def __init__(self):
        ctk.CTk.__init__(self)

        self.closing = False
        self.forge_request_id = 0
        self.report_callback_exception = self._report_callback_exception

        self.title("SoftwareForge")
        self.geometry("1500x900")
        self.minsize(1120, 720)
        self.configure(fg_color=BG)

        self.sandbox = None
        self.builder = None
        self.mode = None  # None | analyze | build
        self.omlx = OMLXClient()
        self.forge = ForgeService(self.omlx)
        self.forge_history = []
        self.forge_phase = "idea"
        self.forge_idea = ""
        self.forge_constitution = ""
        self.forge_spec = ""
        self.forge_plan = ""
        self.forge_tasks = []
        self.forge_build_running = False
        self.forge_status = ""
        self.forge_status_after = None
        self.forge_stream_queue = Queue()
        self.forge_streaming = False
        self.ui_queue = Queue()
        self.forge_project_name = ""
        self.forge_test_result = None
        self.build_canvas_revision = 0

        self.graph = {
            "nodes": [],
            "edges": [],
        }

        self.architecture = {
            "components": [],
            "connections": [],
        }

        self.source = ""
        self.source_kind = ""
        self.commit = ""
        self.project_name = ""
        self.project_key = ""

        self.canvas_path = ""
        self.canvas_states = {}
        self.canvas_widget = None
        self.current_canvas_view = None
        self.selected_component = None
        self.source_cache = {}

        self.brain = WeaviateBrain()
        self.indexer = ProjectIndexer(
            self.brain,
            self.omlx,
        )
        self.agent_service = AgentService(
            self.omlx,
            self.brain,
        )

        self.models = []
        self.chat_models = []
        self.embedding_models = []
        self.reranker_models = []
        self.chat_model = ""
        self.embedding_model = ""
        self.reranker_model = ""

        self.index_state = "Not indexed"
        self.index_ready = False

        self.agent_history = []
        self.last_agent_sources = []
        self.agent_request_started = 0.0
        self.agent_notice = ""
        self.agent_request_token = 0
        self.agent_status_after = None
        self.agent_status_frame = 0
        self.agent_stream_queue = Queue()
        self.agent_streaming = False
        self.sidebar_collapsed = False
        self.current_page = "Project"


        self._sidebar()
        self._shell()
        self.after(25, self._drain_ui_queue)

        self.protocol(
            "WM_DELETE_WINDOW",
            self.close_app,
        )
        self.bind(
            "<Escape>",
            lambda event: self._close_sources(),
            add="+",
        )

        self.after(
            150,
            self._check_docker,
        )

    @property
    def active(self):
        if self.mode == "build":
            return bool(self.builder and self.builder.session_active())

        return bool(
            self.source
            and self.graph.get("nodes")
        )

    @staticmethod
    def _widget_alive(widget):
        try:
            return widget is not None and bool(widget.winfo_exists())
        except (AttributeError, tk.TclError):
            return False

    def _report_callback_exception(self, exc, value, traceback):
        LOGGER.error(
            "Tk callback exception",
            exc_info=(exc, value, traceback),
        )
        if not self.closing and self.mode == "build":
            self._set_forge_status("Something went wrong. See logs/softwareforge.log", RED)

    def _main_after(self, callback, delay=0):
        """Queue a UI callback without touching Tcl from worker threads."""
        if self.closing:
            return
        self.ui_queue.put((max(0, int(delay)), callback))

    def _drain_ui_queue(self):
        if self.closing:
            return
        while True:
            try:
                delay, callback = self.ui_queue.get_nowait()
            except Empty:
                break
            try:
                if delay:
                    self.after(delay, callback)
                else:
                    callback()
            except Exception:
                LOGGER.exception("UI dispatch callback failed")
        try:
            self.after(25, self._drain_ui_queue)
        except tk.TclError:
            return

    def _forge_callback_allowed(self, request_id):
        return not self.closing and request_id == self.forge_request_id

    def _dispatch_forge(self, request_id, callback):
        def dispatch():
            if self._forge_callback_allowed(request_id):
                callback()
            else:
                LOGGER.info("Discarded stale Forge callback request=%s", request_id)

        self._main_after(dispatch)

    def _sidebar(self):
        sidebar = ctk.CTkFrame(
            self,
            width=SIDEBAR_EXPANDED_WIDTH,
            corner_radius=0,
            fg_color=SIDEBAR,
        )

        sidebar.pack(
            side="left",
            fill="y",
        )

        # Children use pack(), so pack propagation is the one that must be disabled.
        # This is what makes the collapsed width actually release space to the main UI.
        sidebar.pack_propagate(False)
        sidebar.grid_propagate(False)
        self.sidebar = sidebar

        sidebar_top = ctk.CTkFrame(
            sidebar,
            fg_color="transparent",
            height=58,
        )
        sidebar_top.pack(fill="x", padx=10, pady=(12, 0))
        sidebar_top.pack_propagate(False)

        self.sidebar_title = ctk.CTkLabel(
            sidebar_top,
            text="SOFTWAREFORGE",
            text_color=TEXT,
            font=(UI_FONT, 15, "bold"),
        )
        self.sidebar_title.pack(side="left", padx=8, pady=(10, 0))

        self.sidebar_toggle = ctk.CTkButton(
            sidebar_top,
            text="‹",
            width=30,
            height=30,
            corner_radius=8,
            fg_color="transparent",
            hover_color=PANEL_3,
            text_color=MUTED,
            font=(UI_FONT, 17),
            command=self.toggle_sidebar,
        )
        self.sidebar_toggle.pack(side="right", pady=(7, 0))

        self.sidebar_subtitle = ctk.CTkLabel(
            sidebar,
            text="repository intelligence",
            text_color=MUTED,
            font=(UI_FONT, 9),
        )
        self.sidebar_subtitle.pack(
            anchor="w",
            padx=18,
            pady=(0, 18),
        )

        self.nav_buttons = {}
        self.sidebar_nav = ctk.CTkFrame(sidebar, fg_color="transparent")
        self.sidebar_nav.pack(fill="x")

        nav_labels = {
            "Project": "▣   Project",
            "Agent": "✦   Agent",
            "Forge": "✦   Forge",
            "Canvas": "◇   Canvas",
        }

        self.nav_icons = {
            "Project": "▣",
            "Agent": "✦",
            "Forge": "✦",
            "Canvas": "◇",
        }

        for page in self.NAV:
            button = ctk.CTkButton(
                self.sidebar_nav,
                text=nav_labels[page],
                anchor="w",
                height=40,
                corner_radius=7,
                fg_color="transparent",
                hover_color=PANEL_2,
                text_color=SOFT,
                command=lambda p=page:
                    self.show(p),
            )

            button.pack(
                fill="x",
                padx=10,
                pady=3,
            )

            self.nav_buttons[
                page
            ] = button

        spacer = ctk.CTkFrame(
            sidebar,
            fg_color="transparent",
        )
        spacer.pack(fill="both", expand=True)
        self.sidebar_spacer = spacer

        self.sandbox_label = ctk.CTkLabel(
            sidebar,
            text="●  Sandbox\n    Checking",
            text_color=MUTED,
            font=(UI_FONT, 9),
        )

        self.sandbox_label.pack(
            anchor="w",
            padx=18,
            pady=(0, 7),
        )

        self.brain_label = ctk.CTkLabel(
            sidebar,
            text="●  Project Brain\n    Idle",
            text_color=MUTED,
            font=(UI_FONT, 9),
        )

        self.brain_label.pack(
            anchor="w",
            padx=18,
            pady=(0, 8),
        )

        self.sidebar_hint = ctk.CTkLabel(
            sidebar,
            text="",
            justify="left",
            text_color="#565e69",
            font=(UI_FONT, 8),
        )
        self.sidebar_hint.pack(
            anchor="w",
            padx=18,
            pady=(0, 18),
        )

    def toggle_sidebar(self):
        self.sidebar_collapsed = not self.sidebar_collapsed
        self._apply_sidebar_state()

    def _apply_sidebar_state(self):
        collapsed = self.sidebar_collapsed
        width = SIDEBAR_COLLAPSED_WIDTH if collapsed else SIDEBAR_EXPANDED_WIDTH

        # IMPORTANT: the sidebar children use pack(), therefore both the frame width
        # and the root grid column must be constrained. Without pack_propagate(False)
        # Tk keeps the old requested width even after the labels are hidden.
        self.sidebar.pack_propagate(False)
        self.sidebar.configure(width=width)
        self.sidebar_toggle.configure(text="›" if collapsed else "‹")

        # Keep widget packing order stable.  Hiding/repacking labels changed their
        # order and let children keep the old requested width on some Tk builds.
        self.sidebar_title.configure(text="" if collapsed else "SOFTWAREFORGE")
        self.sidebar_subtitle.configure(text="" if collapsed else "repository intelligence")
        self.sidebar_hint.configure(text="")
        self.sidebar_subtitle.pack_configure(
            padx=0 if collapsed else 18,
            pady=(0, 6 if collapsed else 18),
        )

        for page, button in self.nav_buttons.items():
            button.configure(
                text=self.nav_icons[page] if collapsed else {
                    "Project": "▣   Project",
                    "Agent": "✦   Agent",
                    "Forge": "✦   Forge",
                    "Canvas": "◇   Canvas",
                }[page],
                anchor="center" if collapsed else "w",
            )
            button.pack_configure(
                padx=7 if collapsed else 10,
                pady=3,
            )

        for label in (self.sandbox_label, self.brain_label):
            if collapsed:
                label.configure(text="●", justify="center")
            else:
                label.configure(
                    text=getattr(label, "expanded_text", label.cget("text")),
                    justify="left",
                )
            label.pack_configure(
                anchor="center" if collapsed else "w",
                padx=0 if collapsed else 18,
            )

        self._refresh_nav()
        self.update_idletasks()

    def _refresh_nav(self):
        if not hasattr(self, "nav_buttons"):
            return

        if self.mode == "build":
            visible = ("Project", "Forge", "Canvas")
        elif self.mode == "analyze":
            visible = ("Project", "Canvas", "Agent")
        else:
            visible = ("Project",)

        for button in self.nav_buttons.values():
            button.pack_forget()

        for page in visible:
            button = self.nav_buttons[page]
            button.pack(
                fill="x",
                padx=7 if self.sidebar_collapsed else 10,
                pady=3,
            )

    def _shell(self):
        self.main = ctk.CTkFrame(
            self,
            fg_color=BG,
            corner_radius=0,
        )

        self.main.pack(
            side="left",
            fill="both",
            expand=True,
        )

        self.main.grid_columnconfigure(
            0,
            weight=1,
        )

        self.main.grid_rowconfigure(
            1,
            weight=1,
        )

        self.header = ctk.CTkFrame(
            self.main,
            height=46,
            fg_color=BG,
            corner_radius=0,
        )

        self.header.grid(
            row=0,
            column=0,
            sticky="ew",
        )

        self.header.grid_propagate(
            False
        )

        self.page_title = ctk.CTkLabel(
            self.header,
            text="Project",
            text_color=TEXT,
            font=(UI_FONT, 11, "bold"),
        )

        self.page_title.pack(
            side="left",
            padx=22,
        )

        self.project_label = ctk.CTkLabel(
            self.header,
            text="",
            text_color=MUTED,
            font=(UI_FONT, 9),
        )

        self.project_label.pack(
            side="right",
            padx=(0, 18),
        )

        self.session_label = ctk.CTkLabel(
            self.header,
            text="SANDBOX ACTIVE",
            text_color=GREEN,
            font=(UI_FONT, 9, "bold"),
        )

        self.end_button = ctk.CTkButton(
            self.header,
            text="End Analysis",
            width=112,
            height=32,
            corner_radius=7,
            fg_color="transparent",
            hover_color="#321a1e",
            border_width=1,
            border_color="#6a3038",
            text_color="#ff8892",
            command=self.end_analysis,
        )

        self.content = ctk.CTkFrame(
            self.main,
            fg_color=BG,
            corner_radius=0,
        )

        self.content.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=PAGE_PAD_X,
            pady=(PAGE_PAD_Y, PAGE_PAD_Y),
        )

        self.show("Project")

    def _check_docker(self):
        try:
            self.sandbox = SandboxManager()
            self.builder = BuilderSandbox()

            analyzer_ok = self.sandbox.image_ready()
            builder_ok = self.builder.image_ready()

            if analyzer_ok and builder_ok:
                self._set_sidebar_status(self.sandbox_label, "Sandbox", "Ready", GREEN)
            elif analyzer_ok:
                self._set_sidebar_status(self.sandbox_label, "Sandbox", "Build image missing", YELLOW)
            else:
                self._set_sidebar_status(self.sandbox_label, "Sandbox", "Analyzer image missing", YELLOW)

        except Exception:
            self._set_sidebar_status(self.sandbox_label, "Sandbox", "Docker unavailable", RED)

    def _set_sidebar_status(self, label, title, detail, color):
        label.expanded_text = "●  %s\n    %s" % (title, detail)
        label.configure(
            text=label.expanded_text
            if not self.sidebar_collapsed
            else "●",
            text_color=color,
        )

    def _save_canvas_state(self):
        if self.canvas_widget is None:
            return

        key = (
            self.canvas_path
            or "__root__"
        )

        self.canvas_states[key] = {
            "positions": dict(
                self.canvas_widget.node_positions
            ),
            "group_boxes": dict(
                self.canvas_widget.group_boxes
            ),
        }

        self.canvas_widget.stop_flow()

    def _clear(self):
        self._save_canvas_state()
        self.canvas_widget = None

        for widget in self.content.winfo_children():
            widget.destroy()

    def _update_header(self):
        self.project_label.configure(
            text=(self.project_name if self.project_name else "")
        )

        if self.active:
            self.session_label.configure(
                text="BUILD SANDBOX" if self.mode == "build" else "SANDBOX ACTIVE"
            )
            self.end_button.configure(
                text="End Build" if self.mode == "build" else "End Analysis"
            )

            if not self.session_label.winfo_ismapped():
                self.session_label.pack(side="right", padx=(0, 14))

            if not self.end_button.winfo_ismapped():
                self.end_button.pack(side="right", padx=(0, 14))
        else:
            self.session_label.pack_forget()
            self.end_button.pack_forget()

    def show(self, page):
        if page == "Forge" and self.mode != "build":
            page = "Project"
        if page == "Agent" and self.mode != "analyze":
            page = "Project"

        if page in ("Canvas", "Agent") and not self.active:
            page = "Forge" if self.mode == "build" else "Project"

        self._clear()
        self._refresh_nav()
        self.current_page = page

        header_context = "Build" if self.mode == "build" else ("Analyze" if self.mode == "analyze" else "")
        self.page_title.configure(text=header_context)

        for name, button in self.nav_buttons.items():
            button.configure(
                fg_color=(PANEL_3 if name == page else "transparent"),
                text_color=(TEXT if name == page else SOFT),
            )

        self._update_header()

        {
            "Project": self.project_page,
            "Agent": self.agents_page,
            "Forge": self.forge_page,
            "Canvas": self.canvas_page,
        }[page]()

    def project_page(self):
        if self.mode is None:
            return self.landing_page()

        if self.mode == "analyze" and not self.active:
            return self.import_page()

        if self.mode == "build" and not self.active:
            return self.build_intro_page()

        stats = graph_stats(self.graph)
        mode_label = "Build workspace" if self.mode == "build" else "Analysis workspace"
        subtitle = self.source or ("build://%s" % self.project_name)

        shell = ctk.CTkFrame(self.content, fg_color="transparent")
        shell.pack(fill="both", expand=True)

        center = ctk.CTkFrame(shell, fg_color="transparent", width=820)
        center.place(relx=0.5, rely=0.18, anchor="n")

        ctk.CTkLabel(
            center,
            text=self.project_name or "SoftwareForge project",
            text_color=TEXT,
            font=(UI_FONT, 26, "bold"),
        ).pack(anchor="w")

        ctk.CTkLabel(
            center,
            text=subtitle,
            text_color=MUTED,
            font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(4, 18))

        status = "● %s" % mode_label
        if self.mode == "build" and self.forge_phase not in ("idea", "spec"):
            status += "  ·  %s" % self.forge_phase.replace("_", " ").title()

        ctk.CTkLabel(
            center,
            text=status,
            text_color=GREEN,
            font=(UI_FONT, 9, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        ctk.CTkLabel(
            center,
            text=(
                "%d files  ·  %d code nodes  ·  %d relationships"
                % (stats["files"], stats["nodes"], stats["edges"])
            ),
            text_color=MUTED,
            font=(UI_FONT, 10),
        ).pack(anchor="w", pady=(0, 22))

        actions = ctk.CTkFrame(center, fg_color="transparent")
        actions.pack(fill="x")

        if self.mode == "build":
            ctk.CTkButton(
                actions,
                text="✦  Open Forge",
                height=44,
                corner_radius=14,
                fg_color=PANEL,
                hover_color=PANEL_2,
                text_color=TEXT,
                command=lambda: self.show("Forge"),
            ).pack(fill="x", pady=4)

        ctk.CTkButton(
            actions,
            text="◇  Open Canvas",
            height=44,
            corner_radius=14,
            fg_color=PANEL,
            hover_color=PANEL_2,
            text_color=TEXT,
            command=lambda: self.show("Canvas"),
        ).pack(fill="x", pady=4)

        if self.mode == "analyze":
            ctk.CTkButton(
                actions,
                text="✦  Ask Agent",
                height=44,
                corner_radius=14,
                fg_color=PANEL,
                hover_color=PANEL_2,
                text_color=TEXT,
                command=lambda: self.show("Agent"),
            ).pack(fill="x", pady=4)

    def landing_page(self):
        shell = ctk.CTkFrame(self.content, fg_color="transparent")
        shell.pack(fill="both", expand=True)

        center = ctk.CTkFrame(shell, fg_color="transparent", width=780)
        center.place(relx=0.5, rely=0.34, anchor="center")

        ctk.CTkLabel(
            center,
            text="What do you want to do?",
            text_color=TEXT,
            font=(UI_FONT, 28, "bold"),
        ).pack(pady=(0, 7))

        ctk.CTkLabel(
            center,
            text="Understand an existing codebase, or build a new one with Forge.",
            text_color=MUTED,
            font=(UI_FONT, 11),
        ).pack(pady=(0, 26))

        row = ctk.CTkFrame(center, fg_color="transparent")
        row.pack(fill="x")

        ctk.CTkButton(
            row,
            text="◇  Analyze repository\nMap an existing project safely",
            width=330,
            height=82,
            anchor="w",
            corner_radius=18,
            fg_color=PANEL,
            hover_color=PANEL_2,
            text_color=TEXT,
            command=self._enter_analyze_mode,
        ).pack(side="left", expand=True, fill="x", padx=(0, 6))

        ctk.CTkButton(
            row,
            text="✦  Build project\nSpec-driven development with Forge",
            width=330,
            height=82,
            anchor="w",
            corner_radius=18,
            fg_color=PANEL,
            hover_color=PANEL_2,
            text_color=TEXT,
            command=self._enter_build_mode,
        ).pack(side="left", expand=True, fill="x", padx=(6, 0))

    def _enter_analyze_mode(self):
        self.mode = "analyze"
        self._refresh_nav()
        self.show("Project")

    def _enter_build_mode(self):
        self.mode = "build"
        self._refresh_nav()
        self.show("Forge")

    def build_intro_page(self):
        shell = ctk.CTkFrame(self.content, fg_color="transparent")
        shell.pack(fill="both", expand=True)

        center = ctk.CTkFrame(shell, fg_color="transparent", width=760)
        center.place(relx=0.5, rely=0.34, anchor="center")

        ctk.CTkLabel(
            center,
            text="Build with Forge",
            text_color=TEXT,
            font=(UI_FONT, 28, "bold"),
        ).pack(pady=(0, 8))

        ctk.CTkLabel(
            center,
            text=(
                "Describe the product once. Forge turns the idea into a specification, "
                "plan and small implementation tasks before it writes code."
            ),
            text_color=MUTED,
            wraplength=650,
            justify="center",
            font=(UI_FONT, 10),
        ).pack(pady=(0, 20))

        ctk.CTkButton(
            center,
            text="Open Forge",
            width=150,
            height=42,
            corner_radius=14,
            fg_color=ACCENT,
            hover_color="#e65d4d",
            text_color="#ffffff",
            command=lambda: self.show("Forge"),
        ).pack()

    def import_page(self):
        ctk.CTkLabel(
            self.content,
            text="Turn a repository into a living software map.",
            text_color=TEXT,
            font=(UI_FONT, 29, "bold"),
        ).pack(
            anchor="w",
            pady=(49, 6),
        )

        ctk.CTkLabel(
            self.content,
            text=(
                "Analyze hostile source inside a disposable sandbox, explore it visually, "
                "and ask a grounded local Agent about the indexed codebase."
            ),
            text_color=MUTED,
            font=(UI_FONT, 10),
        ).pack(
            anchor="w",
            pady=(0, 28),
        )

        card = ctk.CTkFrame(
            self.content,
            fg_color=PANEL,
            corner_radius=12,
            border_width=1,
            border_color=BORDER,
        )

        card.pack(
            fill="x"
        )

        self.repo_entry = ctk.CTkEntry(
            card,
            height=48,
            fg_color="transparent",
            border_width=0,
            placeholder_text=(
                "https://github.com/owner/repository"
            ),
            text_color=TEXT,
        )

        self.repo_entry.pack(
            side="left",
            fill="x",
            expand=True,
            padx=14,
            pady=7,
        )

        ctk.CTkButton(
            card,
            text="Analyze",
            width=102,
            height=36,
            corner_radius=8,
            fg_color=ACCENT,
            hover_color="#e65d4d",
            text_color="#ffffff",
            command=self.analyze_remote,
        ).pack(
            side="right",
            padx=8,
        )

        ctk.CTkButton(
            self.content,
            text="Open local folder",
            width=160,
            height=38,
            corner_radius=8,
            fg_color="transparent",
            hover_color=PANEL_2,
            border_width=1,
            border_color=BORDER,
            text_color=SOFT,
            command=self.analyze_local,
        ).pack(
            anchor="w",
            pady=14,
        )

        self.import_status = ctk.CTkLabel(
            self.content,
            text="",
            text_color=MUTED,
            font=(UI_FONT, 9),
        )

        self.import_status.pack(
            anchor="w",
            pady=8,
        )

    def _set_import_status(self, text, color=MUTED):
        if hasattr(
            self,
            "import_status",
        ):
            self.import_status.configure(
                text=text,
                text_color=color,
            )

    def _thread(self, function, name="worker", on_error=None):
        def guarded():
            try:
                function()
            except Exception as exc:
                LOGGER.exception("Background worker crashed: %s", name)
                if on_error:
                    self._main_after(lambda error=exc: on_error(error))

        threading.Thread(
            target=guarded,
            name=name,
            daemon=True,
        ).start()

    def analyze_remote(self):
        if not self.sandbox:
            self._set_import_status(
                "Docker sandbox is unavailable.",
                RED,
            )
            return

        url = self.repo_entry.get().strip()

        if not url:
            self._set_import_status(
                "Enter a GitHub repository first.",
                RED,
            )
            return

        self._set_import_status(
            "isolating → cloning → cutting network → mapping repository…",
            ACCENT,
        )

        def work():
            try:
                result = self.sandbox.start_github(
                    url
                )

                self._main_after(lambda: self._accept_result(result))
            except Exception as exc:
                self._main_after(
                    lambda error=exc: self._set_import_status(str(error), RED)
                )

        self._thread(work)

    def analyze_local(self):
        folder = filedialog.askdirectory()

        if not folder:
            return

        if not self.sandbox:
            self._set_import_status(
                "Docker sandbox is unavailable.",
                RED,
            )
            return

        self._set_import_status(
            "copying into isolated memory → mapping repository…",
            ACCENT,
        )

        def work():
            try:
                result = self.sandbox.start_local(
                    folder
                )

                self._main_after(lambda: self._accept_result(result))
            except Exception as exc:
                self._main_after(
                    lambda error=exc: self._set_import_status(str(error), RED)
                )

        self._thread(work)

    def _accept_result(self, result):
        self.graph = result["graph"]
        self.source = result["source"]
        self.source_kind = result.get(
            "kind",
            "",
        )
        self.commit = result.get(
            "commit",
            "",
        )

        if self.source_kind == "github":
            name = (
                self.source
                .rstrip("/")
                .split("/")[-1]
            )

            self.project_name = (
                name[:-4]
                if name.endswith(".git")
                else name
            )
        else:
            self.project_name = Path(
                self.source
            ).name

        self.project_key = project_id(
            self.source,
            self.commit,
        )

        self.architecture = build_architecture(
            self.graph,
            self.project_name,
        )

        self.canvas_path = ""
        self.canvas_states = {}
        self.selected_component = None
        self.source_cache = {}
        self.agent_history = []
        self.last_agent_sources = []
        self.agent_notice = ""

        self.index_state = "Waiting for Project Brain"
        self.index_ready = False

        self._set_sidebar_status(self.sandbox_label, "Sandbox", "Active", GREEN)
        self._set_sidebar_status(self.brain_label, "Project Brain", "Preparing", YELLOW)
        self._refresh_nav()
        self.show("Canvas")
        # Index asynchronously so Canvas is immediately usable.
        self.after(250, self.start_indexing)

    def end_analysis(self):
        self.forge_request_id += 1
        self._save_canvas_state()

        if self.sandbox:
            try:
                self.sandbox.end_session()
            except Exception:
                pass

        if self.builder:
            try:
                self.builder.end_session()
            except Exception:
                pass

        self.graph = {"nodes": [], "edges": []}
        self.architecture = {"components": [], "connections": []}
        self.source = ""
        self.source_kind = ""
        self.commit = ""
        self.project_name = ""
        self.project_key = ""
        self.canvas_path = ""
        self.canvas_states = {}
        self.selected_component = None
        self.source_cache = {}
        self.index_state = "Not indexed"
        self.index_ready = False

        self.forge_history = []
        self.forge_phase = "idea"
        self.forge_idea = ""
        self.forge_constitution = ""
        self.forge_spec = ""
        self.forge_plan = ""
        self.forge_tasks = []
        self.forge_build_running = False
        self.forge_status = ""
        self.forge_project_name = ""
        self.forge_test_result = None
        self.agent_history = []
        self.last_agent_sources = []
        self.agent_notice = ""
        self.mode = None

        self._set_sidebar_status(self.sandbox_label, "Sandbox", "Ready", GREEN)
        self._set_sidebar_status(self.brain_label, "Project Brain", "Idle", MUTED)
        self._refresh_nav()
        self.show("Project")

    def close_app(self):
        if self.closing:
            return
        self.closing = True
        self.forge_request_id += 1
        LOGGER.info("Application closing")
        self._save_canvas_state()

        try:
            if self.sandbox:
                self.sandbox.end_session()
            if self.builder:
                self.builder.end_session()
        finally:
            self.destroy()

    def _current_view(self):
        if self.canvas_path:
            return build_folder_view(
                self.graph,
                self.canvas_path,
            )

        return self.architecture

    def _breadcrumb_parts(self):
        if not self.canvas_path:
            return []

        parts = PurePosixPath(
            self.canvas_path
        ).parts

        result = []

        for index, part in enumerate(parts):
            result.append(
                (
                    part,
                    str(
                        PurePosixPath(
                            *parts[: index + 1]
                        )
                    ),
                )
            )

        return result

    def canvas_page(self):
        view = self._current_view()
        self.current_canvas_view = view

        toolbar = ctk.CTkFrame(self.content, fg_color="transparent")
        toolbar.pack(fill="x", pady=(0, 8))

        title_block = ctk.CTkFrame(toolbar, fg_color="transparent")
        title_block.pack(side="left")

        ctk.CTkLabel(
            title_block,
            text="Canvas",
            text_color=TEXT,
            font=(UI_FONT, 24, "bold"),
        ).pack(anchor="w")

        ctk.CTkLabel(
            title_block,
            text=(
                ("Live build architecture · revision %d" % self.build_canvas_revision)
                if self.mode == "build"
                else "Explore the repository as a connected system"
            ),
            text_color=MUTED,
            font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(2, 0))

        controls = ctk.CTkFrame(toolbar, fg_color="transparent")
        controls.pack(side="right", pady=(6, 0))

        search_entry = ctk.CTkEntry(
            controls,
            width=205,
            height=34,
            corner_radius=12,
            fg_color=PANEL,
            border_color=BORDER,
            placeholder_text="Find block / file…",
        )
        search_entry.pack(side="left", padx=(0, 6))

        arrange_button = ctk.CTkButton(
            controls, text="Arrange", width=64, height=34, corner_radius=11,
            fg_color=PANEL, hover_color=PANEL_2, border_width=0, text_color=SOFT,
        )
        arrange_button.pack(side="left", padx=3)

        fit_button = ctk.CTkButton(
            controls, text="Fit", width=54, height=34, corner_radius=11,
            fg_color=PANEL, hover_color=PANEL_2, border_width=0, text_color=SOFT,
        )
        fit_button.pack(side="left", padx=3)

        flow_button = ctk.CTkButton(
            controls, text="Flow", width=60, height=34, corner_radius=11,
            fg_color=PANEL, hover_color=PANEL_2, border_width=0, text_color=SOFT,
        )
        flow_button.pack(side="left", padx=(3, 0))

        breadcrumb = ctk.CTkFrame(self.content, height=30, fg_color="transparent")
        breadcrumb.pack(fill="x", pady=(0, 8))

        if self.canvas_path:
            ctk.CTkButton(
                breadcrumb,
                text="←",
                width=30,
                height=26,
                corner_radius=9,
                fg_color="transparent",
                hover_color=PANEL_3,
                text_color=SOFT,
                command=self._navigate_parent_folder,
            ).pack(side="left", padx=(0, 5))

        ctk.CTkButton(
            breadcrumb,
            text="ROOT",
            width=48,
            height=26,
            corner_radius=8,
            fg_color="transparent",
            hover_color=PANEL_3,
            text_color=ACCENT if not self.canvas_path else MUTED,
            command=lambda: self._navigate_folder(""),
        ).pack(side="left")

        for part, path in self._breadcrumb_parts():
            ctk.CTkLabel(breadcrumb, text="›", text_color=TEXT_DIM).pack(side="left", padx=3)
            ctk.CTkButton(
                breadcrumb,
                text=part,
                height=26,
                corner_radius=8,
                fg_color="transparent",
                hover_color=PANEL_3,
                text_color=SOFT,
                command=lambda p=path: self._navigate_folder(p),
            ).pack(side="left")

        ctk.CTkLabel(
            breadcrumb,
            text="%d blocks · %d links" % (
                len(view.get("components", [])),
                len(view.get("connections", [])),
            ),
            text_color=TEXT_DIM,
            font=(UI_FONT, 8),
        ).pack(side="right")

        body = ctk.CTkFrame(self.content, fg_color="transparent")
        body.pack(fill="both", expand=True)

        canvas_frame = ctk.CTkFrame(
            body,
            fg_color=CANVAS_BG,
            corner_radius=16,
            border_width=1,
            border_color="#2a2a2a",
        )
        canvas_frame.pack(fill="both", expand=True)

        state_key = self.canvas_path or "__root__"
        state = self.canvas_states.get(state_key, {})

        self.canvas_widget = WorkflowCanvas(
            canvas_frame,
            view,
            on_select=self._component_selected,
            on_open=self._component_opened,
            initial_positions=state.get("positions"),
            initial_group_boxes=state.get("group_boxes"),
            show_groups=(view.get("view_kind") == "architecture"),
        )
        self.canvas_widget.pack(fill="both", expand=True, padx=3, pady=3)
        self.canvas_widget.bind("<BackSpace>", lambda event: self._navigate_parent_folder())

        # Small selection HUD. It floats over the graph and never steals layout width.
        self.canvas_selection_hud = ctk.CTkFrame(
            body,
            fg_color="#242424",
            corner_radius=14,
            border_width=1,
            border_color="#3a3a3a",
        )
        self.canvas_selection_hud.place_forget()

        self.canvas_selection_label = ctk.CTkLabel(
            self.canvas_selection_hud,
            text="",
            text_color=TEXT,
            font=(UI_FONT, 9, "bold"),
        )
        self.canvas_selection_label.pack(side="left", padx=(12, 8), pady=8)

        self.canvas_source_button = ctk.CTkButton(
            self.canvas_selection_hud,
            text="Source",
            width=58,
            height=27,
            corner_radius=9,
            fg_color=PANEL,
            hover_color=PANEL_2,
            text_color=SOFT,
            command=self._open_canvas_source_drawer,
        )
        self.canvas_source_button.pack(side="left", padx=3, pady=5)

        if self.mode == "build":
            ctk.CTkButton(
                self.canvas_selection_hud,
                text="Ask Forge",
                width=70,
                height=27,
                corner_radius=9,
                fg_color=PANEL,
                hover_color=PANEL_2,
                text_color=SOFT,
                command=self._ask_selected_in_forge,
            ).pack(side="left", padx=(3, 6), pady=5)
        elif self.mode == "analyze":
            ctk.CTkButton(
                self.canvas_selection_hud,
                text="Ask Agent",
                width=70,
                height=27,
                corner_radius=9,
                fg_color=PANEL,
                hover_color=PANEL_2,
                text_color=SOFT,
                command=self._ask_selected_in_agent,
            ).pack(side="left", padx=(3, 6), pady=5)

        # Floating source drawer: full Canvas remains visible underneath.
        inspector = ctk.CTkFrame(
            body,
            width=560,
            fg_color="#202020",
            corner_radius=16,
            border_width=1,
            border_color="#3a3a3a",
        )
        inspector.place_forget()
        self.canvas_source_drawer = inspector

        inspector_top = ctk.CTkFrame(inspector, fg_color="transparent")
        inspector_top.pack(fill="x", padx=12, pady=(10, 6))

        self.node_title = ctk.CTkLabel(
            inspector_top,
            text="Source",
            text_color=TEXT,
            font=(UI_FONT, 11, "bold"),
        )
        self.node_title.pack(side="left")

        ctk.CTkButton(
            inspector_top,
            text="×",
            width=28,
            height=26,
            corner_radius=9,
            fg_color="transparent",
            hover_color=PANEL_2,
            text_color=MUTED,
            font=(UI_FONT, 15),
            command=self._close_canvas_source_drawer,
        ).pack(side="right")

        if self.mode == "build":
            ctk.CTkButton(
                inspector_top,
                text="Ask Forge",
                width=76,
                height=26,
                corner_radius=9,
                fg_color="transparent",
                hover_color=PANEL_2,
                text_color=SOFT,
                command=self._ask_selected_in_forge,
            ).pack(side="right", padx=4)
        elif self.mode == "analyze":
            ctk.CTkButton(
                inspector_top,
                text="Ask Agent",
                width=76,
                height=26,
                corner_radius=9,
                fg_color="transparent",
                hover_color=PANEL_2,
                text_color=SOFT,
                command=self._ask_selected_in_agent,
            ).pack(side="right", padx=4)

        self.node_meta = ctk.CTkLabel(
            inspector,
            text="",
            justify="left",
            anchor="w",
            wraplength=510,
            text_color=MUTED,
            font=(UI_FONT, 8),
        )
        self.node_meta.pack(fill="x", padx=12, pady=(0, 7))

        self.file_var = ctk.StringVar(value="")
        self.file_menu = ctk.CTkOptionMenu(
            inspector,
            variable=self.file_var,
            values=["No source selected"],
            height=32,
            corner_radius=10,
            fg_color=PANEL,
            button_color=PANEL,
            button_hover_color=PANEL_2,
            dropdown_fg_color=PANEL,
            dropdown_hover_color=PANEL_2,
            dropdown_text_color=TEXT,
            text_color=SOFT,
            state="disabled",
            command=self._file_changed,
        )
        self.file_menu.pack(fill="x", padx=12, pady=(0, 7))

        self.code_status = ctk.CTkLabel(
            inspector,
            text="",
            anchor="w",
            text_color=TEXT_DIM,
            font=(UI_FONT, 8),
        )
        self.code_status.pack(fill="x", padx=12, pady=(0, 4))

        self.code_view = SyntaxCodeView(inspector)
        self.code_view.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.code_view.show_message("Select a node, then open Source.")

        def search(event=None):
            result = self.canvas_widget.focus_search(search_entry.get())
            search_entry.configure(border_color=GREEN if result else RED)

        search_entry.bind("<Return>", search)
        arrange_button.configure(command=self.canvas_widget.auto_arrange)
        fit_button.configure(command=self.canvas_widget.fit_view)

        def toggle_flow():
            running = self.canvas_widget.toggle_flow()
            flow_button.configure(
                text="Stop" if running else "Flow",
                fg_color="#332321" if running else PANEL,
                text_color=ACCENT if running else SOFT,
            )

        flow_button.configure(command=toggle_flow)

        canvas_widget = self.canvas_widget
        self.after(120, lambda widget=canvas_widget: self._fit_canvas_if_alive(widget))

    def _open_canvas_source_drawer(self, explicit_path=None, focus_line=None):
        if not hasattr(self, "canvas_source_drawer"):
            return

        files = getattr(self, "canvas_selected_files", [])
        if explicit_path:
            files = [explicit_path]

        if not files:
            return

        self.file_menu.configure(values=files, state="normal")
        selected = explicit_path or files[0]
        self.file_var.set(selected)

        self.canvas_selection_hud.place_forget()
        self.canvas_source_drawer.configure(width=560)
        self.canvas_source_drawer.place(
            relx=1.0,
            x=-18,
            y=18,
            anchor="ne",
            relheight=0.92,
        )
        self.canvas_source_drawer.lift()
        self._load_source_file(selected, focus_line=focus_line)

    def _open_canvas_source_drawer_at_line(self, path, line):
        self.canvas_selected_files = [path]
        self._open_canvas_source_drawer(path, focus_line=line)

    def _close_canvas_source_drawer(self):
        if hasattr(self, "canvas_source_drawer"):
            self.canvas_source_drawer.place_forget()
        if self.selected_component and hasattr(self, "canvas_selection_hud"):
            self.canvas_selection_hud.place(relx=1.0, x=-18, y=18, anchor="ne")
            self.canvas_selection_hud.lift()

    def _fit_canvas_if_alive(self, canvas_widget):
        try:
            if canvas_widget.winfo_exists():
                canvas_widget.fit_view()
        except tk.TclError:
            pass

    def _navigate_parent_folder(self):
        if not self.canvas_path:
            return

        parent = str(PurePosixPath(self.canvas_path).parent)
        self._navigate_folder("" if parent == "." else parent)

    def _ask_selected_in_forge(self):
        if self.mode != "build":
            return

        if self.selected_component:
            selected = self.selected_component.get(
                "raw_label",
                self.selected_component.get("label", "this module"),
            )
            prompt = (
                "Explain the current build module %s and tell me what Forge should "
                "consider before changing it." % selected
            )
        else:
            prompt = "Explain the current build architecture and the next sensible implementation step."

        self.forge_prefill = prompt
        self.show("Forge")

    def _ask_selected_in_agent(self):
        if self.mode != "analyze":
            return
        if self.selected_component:
            selected = self.selected_component.get(
                "raw_label",
                self.selected_component.get("label", "this module"),
            )
            prompt = "Explain %s, what it owns, and how it connects to the rest of the project." % selected
        else:
            prompt = "Explain this project's architecture and main execution flow."
        self.agent_prefill = prompt
        self.show("Agent")

    def _navigate_folder(self, path):
        self._save_canvas_state()
        self.canvas_path = path
        self.show("Canvas")

    def _component_opened(self, component):
        folder_path = component.get("folder_path", "")

        if folder_path:
            self._navigate_folder(folder_path)
            return

        file_path = component.get("file_path", "")
        if file_path:
            self.selected_component = component
            self.canvas_selected_files = [file_path]
            self._open_canvas_source_drawer(file_path)

    def _component_selected(self, component, edges):
        self.selected_component = component

        incoming = [edge for edge in edges if edge["to"] == component["id"]]
        outgoing = [edge for edge in edges if edge["from"] == component["id"]]

        files = list(component.get("files", []) or [])
        if component.get("file_path"):
            files = [component["file_path"]]
        self.canvas_selected_files = files

        label = component.get("raw_label", component.get("label", "Selected"))
        self.canvas_selection_label.configure(
            text="%s  ·  %s" % (label, component.get("subtitle", ""))
        )
        self.canvas_source_button.configure(
            state="normal" if files else "disabled"
        )

        if hasattr(self, "node_title"):
            self.node_title.configure(text=label)

        if hasattr(self, "node_meta"):
            hint = " · double-click to open" if component.get("folder_path") else ""
            self.node_meta.configure(
                text="%d incoming · %d outgoing%s" % (
                    len(incoming),
                    len(outgoing),
                    hint,
                )
            )

        if hasattr(self, "file_menu"):
            if files:
                self.file_menu.configure(values=files, state="normal")
                self.file_var.set(files[0])
            else:
                self.file_menu.configure(values=["No source files"], state="disabled")
                self.file_var.set("No source files")

        # Selecting a node should never steal half the screen. Show only a compact HUD.
        if hasattr(self, "canvas_source_drawer"):
            self.canvas_source_drawer.place_forget()
        if hasattr(self, "canvas_selection_hud"):
            self.canvas_selection_hud.place(relx=1.0, x=-18, y=18, anchor="ne")
            self.canvas_selection_hud.lift()

    def _file_changed(self, value):
        if value and value not in ("No source files", "No source selected"):
            self._load_source_file(value)

    def _active_source_reader(self):
        if self.mode == "build":
            return self.builder
        return self.sandbox

    def _load_source_file(self, relative_path, focus_line=None):
        if not hasattr(self, "code_view"):
            return

        cache_key = "%s:%s" % (self.mode or "none", relative_path)
        cached = self.source_cache.get(cache_key)

        if cached is not None:
            self._show_source(relative_path, cached, focus_line)
            return

        self.code_status.configure(
            text="reading from isolated container…",
            text_color=ACCENT,
        )

        reader = self._active_source_reader()
        if reader is None:
            self._show_source_error(relative_path, RuntimeError("No active sandbox."))
            return

        def work():
            try:
                source = reader.read_source(relative_path)
                self.source_cache[cache_key] = source

                while len(self.source_cache) > 36:
                    first = next(iter(self.source_cache))
                    self.source_cache.pop(first, None)

                self._main_after(
                    lambda: self._show_source(relative_path, source, focus_line)
                )
            except Exception as exc:
                self._main_after(
                    lambda error=exc: self._show_source_error(relative_path, error)
                )

        self._thread(work)

    def _show_source(
        self,
        relative_path,
        source,
        focus_line=None,
    ):
        self.code_status.configure(
            text=relative_path,
            text_color=GREEN,
        )

        self.code_view.show_source(
            relative_path,
            source,
            focus_line=focus_line,
        )

    def _show_source_error(
        self,
        relative_path,
        error,
    ):
        self.code_status.configure(
            text=relative_path,
            text_color=RED,
        )

        self.code_view.show_message(
            "Could not open source: %s"
            % str(error)
        )

    # ------------------------------------------------------------------
    # Forge — spec-driven Build mode
    # ------------------------------------------------------------------

    def forge_page(self):
        if self.mode != "build":
            self.mode = "build"
            self._refresh_nav()

        top = ctk.CTkFrame(self.content, fg_color="transparent", height=42)
        top.pack(fill="x", pady=(0, 8))
        top.pack_propagate(False)

        ctk.CTkLabel(
            top,
            text="✦ Forge",
            text_color=TEXT,
            font=(UI_FONT, 11, "bold"),
        ).pack(side="left", padx=(2, 10), pady=8)

        phase_names = {
            "idea": "Describe",
            "spec_review": "Specification",
            "plan_review": "Plan",
            "tasks_ready": "Tasks",
            "building": "Building",
            "done": "Converged",
        }
        ctk.CTkLabel(
            top,
            text=phase_names.get(self.forge_phase, self.forge_phase.title()),
            text_color=GREEN if self.forge_phase == "done" else MUTED,
            font=(UI_FONT, 8),
        ).pack(side="left", pady=8)

        self.forge_model_var = ctk.StringVar(value=self.chat_model or "Select oMLX model")
        self.forge_model_menu = ctk.CTkOptionMenu(
            top,
            width=250,
            height=30,
            values=self.chat_models or ([self.chat_model] if self.chat_model else ["Refresh models"]),
            variable=self.forge_model_var,
            fg_color=PANEL,
            button_color=PANEL,
            button_hover_color=PANEL_2,
            dropdown_fg_color=PANEL,
            dropdown_hover_color=PANEL_2,
            dropdown_text_color=TEXT,
            text_color=SOFT,
            command=self._chat_model_changed,
        )
        self.forge_model_menu.pack(side="right", pady=5)

        if self.project_name:
            ctk.CTkLabel(
                top,
                text=self.project_name,
                text_color=TEXT_DIM,
                font=(UI_FONT, 8),
            ).pack(side="right", padx=10)

        chat_area = ctk.CTkFrame(self.content, fg_color="transparent")
        chat_area.pack(fill="both", expand=True)

        self.forge_chat_scroll = ctk.CTkScrollableFrame(
            chat_area,
            fg_color="transparent",
            corner_radius=0,
        )
        self.forge_chat_scroll.pack(fill="both", expand=True)
        self.forge_chat_scroll.bind("<Configure>", self._resize_forge_column)

        if not self.forge_history:
            self.forge_empty = ctk.CTkFrame(self.forge_chat_scroll, fg_color="transparent")
            self.forge_empty.pack(pady=(120, 20))

            ctk.CTkLabel(
                self.forge_empty,
                text="What do you want to build?",
                text_color=TEXT,
                font=(UI_FONT, 24, "bold"),
            ).pack(pady=(0, 6))

            ctk.CTkLabel(
                self.forge_empty,
                text=(
                    "Forge writes a specification first, then a plan and small tasks. "
                    "Code only runs inside the isolated Build sandbox."
                ),
                text_color=MUTED,
                wraplength=620,
                justify="center",
                font=(UI_FONT, 10),
            ).pack(pady=(0, 18))

            suggestions = ctk.CTkFrame(self.forge_empty, fg_color="transparent")
            suggestions.pack()
            for label, prompt in (
                ("Python desktop app", "Build a clean Python desktop application with a simple local data model."),
                ("Local API", "Build a small local REST API with tests and clear module boundaries."),
                ("Developer tool", "Build a developer utility with a minimal UI and strong error handling."),
            ):
                ctk.CTkButton(
                    suggestions,
                    text=label,
                    height=34,
                    corner_radius=12,
                    fg_color=PANEL,
                    hover_color=PANEL_2,
                    text_color=SOFT,
                    command=lambda value=prompt: self._prefill_forge(value),
                ).pack(side="left", padx=4)
        else:
            self._render_forge_history()

        self.forge_status_label = ctk.CTkLabel(
            self.content,
            text=self.forge_status,
            text_color=MUTED,
            font=(UI_FONT, 8),
        )
        self.forge_status_label.pack(pady=(4, 2))

        self.forge_action_bar = ctk.CTkFrame(self.content, fg_color="transparent")
        self.forge_action_bar.pack(fill="x", pady=(0, 4))
        self._render_forge_action()

        composer_wrap = ctk.CTkFrame(self.content, fg_color="transparent")
        composer_wrap.pack(fill="x", pady=(0, 2))
        self.forge_composer_wrap = composer_wrap

        composer = ctk.CTkFrame(
            composer_wrap,
            fg_color="#2f2f2f",
            corner_radius=26,
            border_width=1,
            border_color="#3b3b3b",
            height=62,
        )
        composer.pack(pady=(0, 1))
        composer.pack_propagate(False)
        self.forge_composer = composer

        self.forge_input = ctk.CTkTextbox(
            composer,
            height=42,
            fg_color="#2f2f2f",
            border_width=0,
            corner_radius=18,
            text_color="#f4f4f4",
            font=(UI_FONT, 15),
            wrap="word",
            activate_scrollbars=False,
        )
        self.forge_input.pack(side="left", fill="both", expand=True, padx=(14, 8), pady=8)
        self.forge_input.bind("<Return>", self._forge_enter)

        prefill = getattr(self, "forge_prefill", "")
        if prefill:
            self.forge_input.insert("1.0", prefill)
            self.forge_prefill = ""
        else:
            self.forge_input.insert("1.0", "Describe what you want to build…")
            self.forge_input.configure(text_color=MUTED)
        self.forge_input.bind("<FocusIn>", self._forge_input_focus)
        self.forge_input.bind("<KeyRelease>", self._resize_forge_input)

        self.forge_send_button = ctk.CTkButton(
            composer,
            text="↑",
            width=38,
            height=38,
            corner_radius=19,
            fg_color=ACCENT,
            hover_color="#e65d4d",
            text_color="#ffffff",
            font=(UI_FONT, 17, "bold"),
            command=self.send_forge_message,
        )
        self.forge_send_button.pack(side="right", padx=(0, 10), pady=13)

        self.after(50, self._resize_forge_column)
        if not self.chat_models:
            self.after(120, self.refresh_models)

    def _resize_forge_column(self, event=None):
        if not hasattr(self, "forge_chat_scroll"):
            return
        try:
            width = max(480, self.forge_chat_scroll.winfo_width())
            pad = max(12, int((width - CHAT_MAX_WIDTH) / 2))

            for child in self.forge_chat_scroll.winfo_children():
                try:
                    if child is getattr(self, "forge_empty", None):
                        continue
                    child.pack_configure(padx=pad)
                except Exception:
                    pass

            if hasattr(self, "forge_composer"):
                available = max(480, self.content.winfo_width())
                composer_width = min(CHAT_MAX_WIDTH, max(480, available - 36))
                self.forge_composer.configure(width=composer_width)
        except tk.TclError:
            pass

    def _forge_input_focus(self, event=None):
        if not hasattr(self, "forge_input"):
            return
        value = self.forge_input.get("1.0", tk.END).strip()
        if value == "Describe what you want to build…":
            self.forge_input.delete("1.0", tk.END)
            self.forge_input.configure(text_color="#f4f4f4")

    def _resize_forge_input(self, event=None):
        if not hasattr(self, "forge_input"):
            return
        value = self.forge_input.get("1.0", tk.END)
        line_count = max(1, min(5, value.count("\n") + 1))
        height = 42 + (line_count - 1) * 20
        self.forge_input.configure(height=height)
        if hasattr(self, "forge_composer"):
            self.forge_composer.configure(height=62 + (line_count - 1) * 20)

    def _prefill_forge(self, text):
        if hasattr(self, "forge_input"):
            self.forge_input.delete("1.0", tk.END)
            self.forge_input.insert("1.0", text)
            self.forge_input.configure(text_color="#f4f4f4")
            self.forge_input.focus_set()

    def _forge_enter(self, event):
        if event.state & 0x0001:  # Shift+Enter
            return None
        self.send_forge_message()
        return "break"

    def _render_forge_history(self):
        if not self._widget_alive(getattr(self, "forge_chat_scroll", None)):
            return

        for child in self.forge_chat_scroll.winfo_children():
            child.destroy()

        for item in self.forge_history:
            message = ChatMessage(
                self.forge_chat_scroll,
                role=item.get("role", "assistant"),
                content=item.get("content", ""),
                agent_name="Forge",
                meta=item.get("meta", ""),
            )
            message.pack(fill="x", pady=2)

        LOGGER.info("FORGE render_started messages=%d", len(self.forge_history))
        self._main_after(self._resize_forge_column, 30)
        try:
            self.forge_chat_scroll._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass
        LOGGER.info("FORGE render_completed")

    def _render_forge_action(self):
        if not hasattr(self, "forge_action_bar"):
            return
        for child in self.forge_action_bar.winfo_children():
            child.destroy()

        labels = {
            "spec_review": "Approve specification → Plan",
            "plan_review": "Approve plan → Tasks",
            "tasks_ready": "Approve tasks → Build",
        }
        label = labels.get(self.forge_phase)
        if not label:
            return

        holder = ctk.CTkFrame(self.forge_action_bar, fg_color="transparent")
        holder.pack()
        ctk.CTkButton(
            holder,
            text=label,
            height=34,
            corner_radius=12,
            fg_color=PANEL,
            hover_color=PANEL_2,
            text_color=TEXT,
            command=self.continue_forge,
        ).pack()

    def _set_forge_status(self, text, color=MUTED):
        self.forge_status = text
        if hasattr(self, "forge_status_label"):
            try:
                if self.forge_status_label.winfo_exists():
                    self.forge_status_label.configure(text=text, text_color=color)
            except tk.TclError:
                pass

    def _ensure_chat_model(self):
        if self.chat_model:
            return self.chat_model
        models = self.omlx.models()
        groups = self.omlx.classify_models(models)
        self.models = models
        self.chat_models = groups["chat"]
        chosen = self.omlx.choose_models(models)
        self.chat_model = chosen["chat"]
        if not self.chat_model:
            raise RuntimeError("No chat model is loaded in oMLX.")
        return self.chat_model

    def _begin_forge_operation(self, status, stream=False):
        if self.forge_build_running or self.closing:
            return None

        try:
            model = self._ensure_chat_model()
        except Exception as exc:
            self._forge_failed(exc)
            return None

        self.forge_request_id += 1
        request_id = self.forge_request_id
        self.forge_build_running = True
        LOGGER.info("FORGE request=%s phase=%s start", request_id, self.forge_phase)
        LOGGER.info("FORGE request=%s model=%s generation_started", request_id, model)
        self._set_forge_status(status, ACCENT)
        if stream:
            self.forge_streaming = True
            self.forge_history.append({
                "role": "assistant",
                "content": status,
                "agent": "Forge",
                "streaming": True,
                "status_placeholder": True,
            })
            if self.current_page == "Forge":
                self._render_forge_history()
                self._poll_forge_stream()
        if hasattr(self, "forge_send_button"):
            self.forge_send_button.configure(state="disabled", text="…")
        return request_id, model

    def send_forge_message(self):
        if self.forge_build_running:
            return

        text = self.forge_input.get("1.0", "end-1c").strip() if hasattr(self, "forge_input") else ""
        if not text or text == "Describe what you want to build…":
            return

        self.forge_input.delete("1.0", tk.END)
        self.forge_history.append({"role": "user", "content": text})
        self._render_forge_history()

        if self.forge_phase == "idea":
            self.forge_idea = text
            self._generate_forge_spec()
            return

        # During review, free-form feedback regenerates the current artifact instead
        # of letting the coding model silently change an approved artifact.
        if self.forge_phase == "spec_review":
            self.forge_idea += "\n\nRevision request: " + text
            self._generate_forge_spec()
        elif self.forge_phase == "plan_review":
            self.forge_spec += "\n\nUser planning note: " + text
            self._generate_forge_plan()
        elif self.forge_phase == "tasks_ready":
            self.forge_plan += "\n\nTask planning note: " + text
            self._generate_forge_tasks()
        else:
            self.forge_history.append({
                "role": "assistant",
                "content": (
                    "The build workflow is already running or complete. Use **Canvas** to inspect "
                    "the live modules, or start a new build after ending this session."
                ),
            })
            self._render_forge_history()

    def continue_forge(self):
        if self.forge_build_running:
            return
        if self.forge_phase == "spec_review":
            self._generate_forge_plan()
        elif self.forge_phase == "plan_review":
            self._generate_forge_tasks()
        elif self.forge_phase == "tasks_ready":
            self._run_forge_build()

    def _ensure_forge_builder(self):
        if self.builder is None:
            self.builder = BuilderSandbox()
        if not self.builder.session_active():
            name_source = self.forge_idea.splitlines()[0] if self.forge_idea else "project"
            project_name = self.forge.slugify(name_source)
            self.builder.start(project_name)
            self.forge_project_name = project_name
            self.project_name = project_name
            self.source = "build://%s" % project_name
            self.source_kind = "build"
            self.commit = "build-%d" % int(time.time() * 1000)
            self.project_key = project_id(self.source, self.commit)
        return self.builder

    def _spec_kit_guidance(self, stage):
        # Spec Kit is enforced in the isolated Build sandbox, but specification
        # chat should still work even if the user has not rebuilt that image yet.
        # This keeps "describe → spec → plan → tasks" usable and surfaces the
        # builder preflight only when implementation actually begins.
        try:
            builder = self._ensure_forge_builder()
            guidance = builder.spec_kit_guidance(stage)
            LOGGER.info(
                "Spec Kit guidance stage=%s chars=%d version=%s",
                stage,
                len(guidance),
                builder.spec_kit_version(),
            )
            return guidance
        except Exception as exc:
            LOGGER.warning("Spec Kit guidance unavailable for %s: %s", stage, exc)
            return ""

    def _remember_project(self, kind, memory_key, content, path=""):
        if not self.project_key or not content or not self.brain.ready():
            return
        project_key = self.project_key
        stamp = str(int(time.time()))
        def work():
            try:
                self.brain.remember(project_key, kind, memory_key, content, path=path, created_at=stamp)
            except Exception:
                LOGGER.exception("Project memory write failed kind=%s", kind)
        self._thread(work, name="memory-%s" % kind)

    def _persist_spec_artifacts_async(self):
        def work():
            try:
                self._write_spec_artifacts()
            except Exception:
                LOGGER.exception("Could not persist Spec/Plan/Tasks artifacts")
        self._thread(work, name="persist-spec-artifacts")

    def _write_spec_artifacts(self):
        if not self.builder or not self.builder.session_active():
            return
        self.builder.write_file(".softwareforge/constitution.md", self.forge_constitution)
        self.builder.write_file(".softwareforge/spec.md", self.forge_spec)
        self.builder.write_file(".softwareforge/plan.md", self.forge_plan)
        self.builder.write_file(".softwareforge/tasks.md", self.forge.task_markdown(self.forge_tasks))
        # Mirror the artifacts into a stable feature directory so the official
        # Spec Kit scaffold and SoftwareForge's own UI tell the same story.
        self.builder.write_file("specs/001-softwareforge/spec.md", self.forge_spec)
        if self.forge_plan:
            self.builder.write_file("specs/001-softwareforge/plan.md", self.forge_plan)
        if self.forge_tasks:
            self.builder.write_file("specs/001-softwareforge/tasks.md", self.forge.task_markdown(self.forge_tasks))
        if self.forge_constitution:
            self.builder.write_file(".specify/memory/constitution.md", self.forge_constitution)

    def _queue_forge_token(self, token):
        self.forge_stream_queue.put(token)

    def _poll_forge_stream(self):
        if self.closing:
            return
        parts = []
        while True:
            try:
                parts.append(self.forge_stream_queue.get_nowait())
            except Empty:
                break
        if parts and self.forge_history:
            current = self.forge_history[-1]
            if current.get("role") == "assistant" and current.get("streaming"):
                if current.pop("status_placeholder", False):
                    current["content"] = ""
                current["content"] += "".join(parts)
                self._set_forge_status("Generating…", ACCENT)
                if self.current_page == "Forge":
                    self._render_forge_history()
        if self.forge_streaming or parts:
            self._main_after(self._poll_forge_stream, 100)

    def _finish_forge_stream_message(self, final_content):
        self.forge_streaming = False
        if self.forge_history and self.forge_history[-1].get("streaming"):
            item = self.forge_history[-1]
            item["content"] = final_content
            item.pop("streaming", None)
            item.pop("status_placeholder", None)
            return True
        return False

    def _generate_forge_spec(self):
        operation = self._begin_forge_operation("Thinking about the specification…", stream=True)
        if operation is None:
            return
        request_id, model = operation
        idea = self.forge_idea

        def work():
            try:
                guidance = self._spec_kit_guidance("specify")
                spec = self.forge.generate_spec(
                    idea, model, on_token=self._queue_forge_token, guidance=guidance
                )
                self._dispatch_forge(
                    request_id,
                    lambda: self._forge_spec_ready(spec, request_id),
                )
            except Exception as exc:
                self._dispatch_forge(
                    request_id,
                    lambda error=exc: self._forge_failed(error),
                )

        self._thread(work, name="forge-spec")

    def _forge_spec_ready(self, spec, request_id=None):
        if not isinstance(spec, str) or not spec.strip():
            self._forge_failed(RuntimeError("Forge returned an empty specification."))
            return
        LOGGER.info("FORGE request=%s completed chars=%d", request_id, len(spec))
        self.forge_constitution = self.forge.spec_engine.constitution()
        self.forge_spec = spec
        self.forge_phase = "spec_review"
        self.forge_build_running = False
        final_message = "## Specification\n\n" + spec
        if not self._finish_forge_stream_message(final_message):
            self.forge_history.append({"role": "assistant", "content": final_message})
        self._persist_spec_artifacts_async()
        self._remember_project("specification", "current-spec", spec, path="specs/001-softwareforge/spec.md")
        self._set_forge_status("Specification ready · review before planning", GREEN)
        if hasattr(self, "forge_send_button"):
            self.forge_send_button.configure(state="normal", text="↑")
        if self.current_page == "Forge":
            self._render_forge_history()
            self._render_forge_action()

    def _generate_forge_plan(self):
        operation = self._begin_forge_operation("Planning the architecture…", stream=True)
        if operation is None:
            return
        request_id, model = operation
        spec = self.forge_spec

        def work():
            try:
                guidance = self._spec_kit_guidance("plan")
                plan = self.forge.generate_plan(
                    spec, model, on_token=self._queue_forge_token, guidance=guidance
                )
                self._dispatch_forge(
                    request_id,
                    lambda: self._forge_plan_ready(plan, request_id),
                )
            except Exception as exc:
                self._dispatch_forge(
                    request_id,
                    lambda error=exc: self._forge_failed(error),
                )

        self._thread(work, name="forge-plan")

    def _forge_plan_ready(self, plan, request_id=None):
        if not isinstance(plan, str) or not plan.strip():
            self._forge_failed(RuntimeError("Forge returned an empty implementation plan."))
            return
        LOGGER.info("FORGE request=%s completed chars=%d", request_id, len(plan))
        self.forge_plan = plan
        self.forge_phase = "plan_review"
        self.forge_build_running = False
        final_message = "## Implementation plan\n\n" + plan
        if not self._finish_forge_stream_message(final_message):
            self.forge_history.append({"role": "assistant", "content": final_message})
        self._persist_spec_artifacts_async()
        self._remember_project("plan", "current-plan", plan, path="specs/001-softwareforge/plan.md")
        self._set_forge_status("Plan ready · approve it before task generation", GREEN)
        if hasattr(self, "forge_send_button"):
            self.forge_send_button.configure(state="normal", text="↑")
        if self.current_page == "Forge":
            self._render_forge_history()
            self._render_forge_action()

    def _generate_forge_tasks(self):
        operation = self._begin_forge_operation("Generating small dependency-ordered tasks…")
        if operation is None:
            return
        request_id, model = operation
        spec = self.forge_spec
        plan = self.forge_plan

        def work():
            try:
                guidance = self._spec_kit_guidance("tasks")
                tasks = self.forge.generate_tasks(spec, plan, model, guidance=guidance)
                if not tasks:
                    raise RuntimeError("Forge could not derive actionable tasks from the plan.")
                self._dispatch_forge(
                    request_id,
                    lambda: self._forge_tasks_ready(tasks, request_id),
                )
            except Exception as exc:
                self._dispatch_forge(
                    request_id,
                    lambda error=exc: self._forge_failed(error),
                )

        self._thread(work, name="forge-tasks")

    def _forge_tasks_ready(self, tasks, request_id=None):
        LOGGER.info("FORGE request=%s completed tasks=%d", request_id, len(tasks))
        self.forge_tasks = tasks
        self.forge_phase = "tasks_ready"
        self.forge_build_running = False
        task_markdown = self.forge.task_markdown(tasks)
        self.forge_history.append({
            "role": "assistant",
            "content": task_markdown,
        })
        self._persist_spec_artifacts_async()
        self._remember_project("tasks", "current-tasks", task_markdown, path="specs/001-softwareforge/tasks.md")
        self._set_forge_status("Tasks ready · approval starts isolated implementation", GREEN)
        if hasattr(self, "forge_send_button"):
            self.forge_send_button.configure(state="normal", text="↑")
        if self.current_page == "Forge":
            self._render_forge_history()
            self._render_forge_action()

    def _run_forge_build(self):
        self.forge_build_running = True
        self.forge_phase = "building"
        self._render_forge_action()
        self._set_forge_status("Starting isolated Build sandbox…", ACCENT)
        if hasattr(self, "forge_send_button"):
            self.forge_send_button.configure(state="disabled", text="…")

        def work():
            try:
                self._ensure_forge_builder()
                self._main_after(self._build_session_started)

                # Keep both SoftwareForge and real Spec Kit artifacts durable inside
                # the isolated project before implementation begins.
                self._write_spec_artifacts()
                self.builder.checkpoint("Approve SoftwareForge specification")

                model = self.chat_model or self._ensure_chat_model()

                for index, task in enumerate(self.forge_tasks, 1):
                    self._main_after(
                        lambda i=index, t=task: self._set_forge_status(
                            "Building %s · %d/%d · %s" % (t["id"], i, len(self.forge_tasks), t["title"]),
                            ACCENT,
                        )
                    )

                    result = self.forge.implement_task(
                        self.builder,
                        task,
                        self.forge_spec,
                        self.forge_plan,
                        model,
                    )
                    task["status"] = "done"
                    self.builder.write_file(".softwareforge/tasks.md", self.forge.task_markdown(self.forge_tasks))
                    self.builder.checkpoint("%s %s" % (task["id"], task["title"]))
                    self.source_cache = {}

                    try:
                        graph = self.builder.analyze_graph()
                    except Exception:
                        graph = {"nodes": [], "edges": []}

                    self._main_after(
                        lambda g=graph, t=task, r=result: self._forge_task_applied(g, t, r)
                    )

                self._main_after(lambda: self._set_forge_status("Verifying build…", ACCENT))
                test_result = self.builder.run_tests()
                report = self.forge.convergence_report(
                    self.forge_spec,
                    self.forge_plan,
                    self.forge_tasks,
                    test_result,
                    model,
                )
                self._main_after(lambda: self._forge_build_done(test_result, report))
            except Exception as exc:
                self._main_after(lambda error=exc: self._forge_failed(error))

        self._thread(work)

    def _build_session_started(self):
        self._set_sidebar_status(self.sandbox_label, "Sandbox", "Build active", GREEN)
        self._set_sidebar_status(self.brain_label, "Live Canvas", "Waiting for modules", YELLOW)
        self._refresh_nav()
        self._update_header()

    def _forge_task_applied(self, graph, task, result):
        if graph.get("nodes"):
            self.graph = graph
            self.architecture = build_architecture(graph, self.project_name or "Build")
            self.canvas_path = ""
            # Preserve manual positions for existing module ids; new modules receive defaults.
            self.build_canvas_revision += 1

        self._set_sidebar_status(self.sandbox_label, "Sandbox", "Build active", GREEN)
        self._set_sidebar_status(
            self.brain_label,
            "Live Canvas",
            "%d modules" % len(self.architecture.get("components", [])),
            GREEN,
        )

        self.forge_history.append({
            "role": "assistant",
            "content": (
                "**%s complete** — %s\n\nChanged: %s\n\nCanvas revision **%d** is ready."
                % (
                    task["id"],
                    result.get("summary", task["title"]),
                    ", ".join("`%s`" % path for path in result.get("files", [])),
                    self.build_canvas_revision,
                )
            ),
        })
        self._remember_project(
            "build_event",
            task.get("id", "task"),
            "%s — %s; files: %s" % (
                task.get("title", "task"),
                result.get("summary", "completed"),
                ", ".join(result.get("files", [])),
            ),
            path=", ".join(result.get("files", [])),
        )
        if self.current_page == "Forge":
            self._render_forge_history()

        # If the user is watching Canvas during a build, refresh it after each task.
        # This creates the live module-building effect without polling the container.
        if self.current_page == "Canvas":
            self.after(40, lambda: self.show("Canvas"))

    def _forge_build_done(self, test_result, report):
        self.forge_test_result = test_result
        self.forge_phase = "done"
        self.forge_build_running = False
        self.forge_history.append({
            "role": "assistant",
            "content": (
                "## Verification\n\n%s\n\n"
                "**Automated tests:** %s"
                % (report, "passed" if test_result.get("ok") else "need attention")
            ),
        })
        self._set_forge_status("Build complete · indexing final project…", GREEN)
        self._remember_project(
            "verification",
            "latest-verification",
            report + "\nTests: " + ("passed" if test_result.get("ok") else "need attention"),
        )
        self.index_ready = False
        self.index_state = "Indexing built project"
        if hasattr(self, "forge_send_button"):
            self.forge_send_button.configure(state="normal", text="↑")
        if self.current_page == "Forge":
            self._render_forge_history()
            self._render_forge_action()
        self._update_header()
        self._main_after(self.start_indexing, 100)

    def _forge_failed(self, error):
        LOGGER.exception("FORGE failed", exc_info=(type(error), error, error.__traceback__))
        self.forge_build_running = False
        self.forge_streaming = False
        self._set_forge_status("Forge needs attention", RED)
        error_message = "**Forge error**\n\n%s" % str(error)
        if self.forge_history and self.forge_history[-1].get("streaming"):
            item = self.forge_history[-1]
            item["content"] = error_message
            item.pop("streaming", None)
            item.pop("status_placeholder", None)
        else:
            self.forge_history.append({"role": "assistant", "content": error_message})
        if hasattr(self, "forge_send_button"):
            try:
                self.forge_send_button.configure(state="normal", text="↑")
            except Exception:
                pass
        if self.current_page == "Forge":
            self._render_forge_history()
            self._render_forge_action()

    # ------------------------------------------------------------------
    # Project Brain
    # ------------------------------------------------------------------

    def start_indexing(self):
        if not self.active or self.index_ready:
            return

        if not self.brain.ready():
            self.index_state = "Weaviate offline — run docker compose up -d"
            self._set_sidebar_status(self.brain_label, "Project Brain", "Offline", RED)
            return

        self.index_state = "Connecting to oMLX…"
        self._set_sidebar_status(self.brain_label, "Project Brain", "Connecting", YELLOW)

        def work():
            try:
                models = self.omlx.models()
                chosen = self.omlx.choose_models(models)
                groups = self.omlx.classify_models(models)

                self.models = models
                self.chat_models = groups["chat"]
                self.embedding_models = groups["embedding"]
                self.reranker_models = groups["reranker"]

                self.chat_model = self.chat_model or chosen["chat"]
                self.embedding_model = self.embedding_model or chosen["embedding"]
                self.reranker_model = self.reranker_model or chosen["reranker"]

                if not self.embedding_model:
                    raise RuntimeError(
                        "oMLX is reachable, but no embedding model was detected."
                    )

                # Same repository + commit uses the same project key. Reopening it
                # should be instant instead of embedding every file again.
                existing = self.brain.project_count(self.project_key)

                if existing > 0:
                    self._main_after(
                        lambda m=models, c=chosen, n=existing: self._index_reused(m, c, n)
                    )
                    return

                def source_progress(stage, current, total):
                    self._main_after(
                        lambda s=stage, c=current, t=total: self._index_progress("%s %d/%d" % (s, c, t))
                    )

                reader = self.builder if self.mode == "build" else self.sandbox
                if reader is None:
                    raise RuntimeError("No active source sandbox is available for indexing.")
                chunks = self.indexer.build_chunks(
                    self.graph,
                    reader,
                    progress=source_progress,
                )

                if not chunks:
                    raise RuntimeError("No text source chunks were available to index.")

                def vector_progress(current, total):
                    self._main_after(
                        lambda c=current, t=total: self._index_progress(
                            "Embedding + Weaviate %d/%d" % (c, t)
                        )
                    )

                self.brain.index_chunks(
                    self.project_key,
                    chunks,
                    self.omlx,
                    self.embedding_model,
                    progress=vector_progress,
                )

                self._main_after(
                    lambda m=models, c=chosen, n=len(chunks): self._index_built(m, c, n)
                )

            except Exception as exc:
                self._main_after(lambda error=exc: self._index_failed(error))

        self._thread(work)

    def _index_reused(self, models, chosen, chunk_count):
        self._models_ready(models, chosen)
        self.index_state = "%d chunks ready · cached" % chunk_count
        self.index_ready = True
        self._set_sidebar_status(
            self.brain_label,
            "Project Brain",
            "%d chunks ready" % chunk_count,
            GREEN,
        )

        self._set_agent_brain_status("● Brain ready", GREEN)

    def _index_built(self, models, chosen, chunk_count):
        self._models_ready(models, chosen)
        self._index_complete(chunk_count)

    def _index_progress(self, text):
        self.index_state = text

        self._set_sidebar_status(self.brain_label, "Project Brain", text[:30], YELLOW)

        self._set_agent_brain_status("● Indexing", YELLOW)

    def _index_complete(self, chunk_count):
        self.index_state = (
            "%d chunks indexed"
            % chunk_count
        )

        self.index_ready = True

        self._set_sidebar_status(
            self.brain_label,
            "Project Brain",
            "%d chunks ready" % chunk_count,
            GREEN,
        )

        self._set_agent_brain_status("● Brain ready", GREEN)

        self._refresh_agent_model_widgets()

    def _index_failed(self, error):
        self.index_state = str(error)
        self.index_ready = False

        self._set_sidebar_status(self.brain_label, "Project Brain", "Needs setup", RED)

        self._set_agent_brain_status("● Brain offline", RED)


    # ------------------------------------------------------------------
    # Agents tab
    # ------------------------------------------------------------------

    def agents_page(self):
        if self.mode != "analyze":
            self.show("Project")
            return
        self.page_title.configure(text="")

        topbar = ctk.CTkFrame(self.content, fg_color="transparent", height=44)
        topbar.pack(fill="x", pady=(0, 8))
        topbar.pack_propagate(False)

        self.agent_name_var = ctk.StringVar(value="Architect")
        self.agent_selector_button = ctk.CTkButton(
            topbar,
            text="Architect  ▾",
            width=145,
            height=34,
            anchor="w",
            corner_radius=9,
            fg_color="transparent",
            hover_color=PANEL_3,
            text_color=TEXT,
            font=(UI_FONT, 11, "bold"),
            command=self._show_agent_menu,
        )
        self.agent_selector_button.pack(side="left")

        ctk.CTkLabel(
            topbar,
            text=self.project_name,
            text_color=MUTED,
            font=(UI_FONT, 9),
        ).pack(side="left", padx=10)

        options = ctk.CTkButton(
            topbar,
            text="•••",
            width=36,
            height=32,
            corner_radius=9,
            fg_color="transparent",
            hover_color=PANEL_3,
            text_color=MUTED,
            font=(UI_FONT, 12, "bold"),
            command=self._show_agent_options,
        )
        options.pack(side="right")

        self.chat_model_var = ctk.StringVar(value=self.chat_model or "chat model")
        self.embed_model_var = ctk.StringVar(value=self.embedding_model or "embedding model")
        self.chat_model_menu = ctk.CTkOptionMenu(
            topbar,
            width=190,
            height=30,
            values=self.chat_models or ["Refresh oMLX models"],
            variable=self.chat_model_var,
            fg_color=PANEL,
            button_color=PANEL,
            button_hover_color=PANEL_2,
            text_color=MUTED,
            command=self._chat_model_changed,
        )
        self.chat_model_menu.pack(side="right", padx=(0, 8))

        self.agent_brain_status = ctk.CTkLabel(
            topbar,
            text="● Brain ready" if self.index_ready else "● Brain",
            text_color=GREEN if self.index_ready else MUTED,
            font=(UI_FONT, 8),
        )
        self.agent_brain_status.pack(side="right", padx=10)

        self.agent_depth_var = ctk.StringVar(value="Fast")
        self.agent_chat_viewport = ctk.CTkFrame(
            self.content,
            fg_color="transparent",
        )
        self.agent_chat_viewport.pack(fill="both", expand=True, padx=0, pady=0)
        self.agent_chat_viewport.bind(
            "<Configure>",
            self._sources_resized,
        )
        self.agent_chat_scroll = ctk.CTkScrollableFrame(
            self.agent_chat_viewport,
            fg_color="transparent",
            corner_radius=0,
        )
        self.agent_chat_scroll.pack(fill="both", expand=True, padx=0, pady=0)
        self.agent_chat_column = ctk.CTkFrame(
            self.agent_chat_scroll,
            fg_color="transparent",
            width=CHAT_MAX_WIDTH,
        )
        self.agent_chat_column.pack(anchor="center", fill="x", padx=CHAT_SIDE_PADDING)
        self.agent_chat_viewport.bind(
            "<Configure>",
            self._resize_agent_column,
            add=True,
        )
        self.after(0, self._resize_agent_column)

        self.sources_floating_button = ctk.CTkButton(
            self.agent_chat_viewport,
            text="Sources",
            width=112,
            height=30,
            corner_radius=10,
            fg_color=SURFACE,
            hover_color=SURFACE_HOVER,
            text_color=SOFT,
            font=(UI_FONT, 8, "bold"),
            command=self._toggle_sources,
        )
        self.sources_floating_button.place(relx=1.0, x=-18, y=12, anchor="ne")

        self.sources_overlay = ctk.CTkFrame(
            self.agent_chat_viewport,
            width=350,
            height=460,
            corner_radius=16,
            fg_color=SURFACE,
            border_width=1,
            border_color=BORDER,
        )
        self.sources_overlay.pack_propagate(False)
        self.sources_overlay.place_forget()

        composer_shell = ctk.CTkFrame(
            self.content,
            fg_color="transparent",
        )
        composer_shell.pack(fill="x", pady=(8, 0))

        composer = ctk.CTkFrame(
            composer_shell,
            width=CHAT_MAX_WIDTH,
            height=62,
            fg_color="#2f2f2f",
            corner_radius=24,
            border_width=1,
            border_color=BORDER,
        )
        composer.pack(anchor="center")
        composer.pack_propagate(False)
        self.agent_composer = composer

        def resize_composer(event):
            composer.configure(
                width=max(300, min(CHAT_MAX_WIDTH, event.width - CHAT_SIDE_PADDING * 2))
            )

        composer_shell.bind("<Configure>", resize_composer)

        self.agent_input = ctk.CTkTextbox(
            composer,
            height=44,
            fg_color="#2f2f2f",
            border_width=0,
            corner_radius=20,
            text_color="#f4f4f4",
            font=(UI_FONT, 15),
            wrap="word",
            activate_scrollbars=False,
        )
        self.agent_input.pack(side="left", fill="x", expand=True, padx=(16, 6), pady=6)
        agent_prefill = getattr(self, "agent_prefill", "")
        if agent_prefill:
            self.agent_input.insert("1.0", agent_prefill)
            self.agent_prefill = ""
        else:
            self.agent_input.insert("1.0", "Ask anything about %s" % self.project_name)
            self.agent_input.configure(text_color=MUTED)
        self.agent_input.bind("<FocusIn>", self._agent_input_focus)
        self.agent_input.bind("<Return>", self._agent_enter)
        self.agent_input.bind("<KeyRelease>", self._resize_agent_input)

        self.agent_send_button = ctk.CTkButton(
            composer,
            text="↑",
            width=36,
            height=36,
            corner_radius=18,
            fg_color=ACCENT,
            hover_color="#e65d4d",
            text_color="#ffffff",
            font=(UI_FONT, 17, "bold"),
            command=self.send_agent_message,
        )
        self.agent_send_button.pack(side="right", padx=(0, 10), pady=8)

        self.agent_runtime_status = ctk.CTkLabel(
            self.content,
            text="",
            text_color=TEXT_DIM,
            font=(UI_FONT, 8),
        )
        self.agent_runtime_status.pack(anchor="e", pady=(3, 0))

        self._render_agent_history()
        self._render_agent_sources()

        if self.models:
            self._refresh_agent_model_widgets()
        else:
            self.after(120, self.refresh_models)
        return

    def _show_agent_menu(self):
        menu = ctk.CTkToplevel(self)
        menu.title("Agent")
        menu.geometry("300x310")
        menu.resizable(False, False)
        menu.configure(fg_color=SIDEBAR)
        menu.transient(self)

        ctk.CTkLabel(
            menu,
            text="Choose an agent",
            text_color=MUTED,
            font=(UI_FONT, 9),
        ).pack(anchor="w", padx=16, pady=(15, 8))

        descriptions = {
            "Architect": "Understand architecture and system flow",
            "Code Guide": "Explain implementation and source",
            "Builder": "Plan changes without executing code",
            "Impact": "Understand what a change may affect",
            "Security": "Review trust boundaries and risky patterns",
        }

        for name, description in descriptions.items():
            button = ctk.CTkButton(
                menu,
                text="%s\n%s" % (name, description),
                anchor="w",
                height=45,
                corner_radius=8,
                fg_color="transparent",
                hover_color=PANEL_3,
                text_color=TEXT,
                font=(UI_FONT, 9),
                command=lambda value=name: self._choose_agent(value, menu),
            )
            button.pack(fill="x", padx=8, pady=1)

    def _choose_agent(self, name, menu):
        self.agent_name_var.set(name)
        self.agent_selector_button.configure(text="%s  ▾" % name)
        menu.destroy()

    def _show_agent_options(self):
        menu = ctk.CTkToplevel(self)
        menu.title("Options")
        menu.geometry("330x370")
        menu.resizable(False, False)
        menu.configure(fg_color=SIDEBAR)
        menu.transient(self)

        ctk.CTkLabel(
            menu,
            text="Retrieval",
            text_color=MUTED,
            font=(UI_FONT, 9),
        ).pack(anchor="w", padx=14, pady=(14, 5))

        ctk.CTkSegmentedButton(
            menu,
            values=["Fast", "Deep"],
            variable=self.agent_depth_var,
            height=30,
            corner_radius=8,
            selected_color=ACCENT,
            selected_hover_color="#e65d4d",
            unselected_color=PANEL,
            unselected_hover_color=PANEL_2,
        ).pack(fill="x", padx=12, pady=(0, 8))

        ctk.CTkLabel(
            menu,
            text="Embedding model",
            text_color=MUTED,
            font=(UI_FONT, 8, "bold"),
        ).pack(anchor="w", padx=14, pady=(7, 4))

        embed_menu = ctk.CTkOptionMenu(
            menu,
            width=300,
            height=30,
            values=self.embedding_models or ["Refresh oMLX models"],
            variable=self.embed_model_var,
            fg_color=SURFACE,
            button_color=SURFACE,
            button_hover_color=SURFACE_HOVER,
            dropdown_fg_color=SURFACE,
            dropdown_hover_color=SURFACE_HOVER,
            dropdown_text_color=TEXT,
            text_color=SOFT,
            command=self._embedding_model_changed,
        )
        embed_menu.pack(padx=12, pady=(0, 8))

        ctk.CTkLabel(
            menu,
            text="Project Brain\n%s" % self.index_state,
            justify="left",
            anchor="w",
            text_color=GREEN if self.index_ready else MUTED,
            font=(UI_FONT, 8),
        ).pack(fill="x", padx=14, pady=(3, 8))

        ctk.CTkButton(
            menu,
            text="Refresh Project Brain",
            height=30,
            corner_radius=8,
            fg_color="transparent",
            hover_color=PANEL_3,
            text_color=SOFT,
            command=lambda: (menu.destroy(), self._force_reindex()),
        ).pack(fill="x", padx=8, pady=2)

        ctk.CTkButton(
            menu,
            text="Refresh models",
            height=30,
            corner_radius=8,
            fg_color="transparent",
            hover_color=PANEL_3,
            text_color=SOFT,
            command=lambda: (menu.destroy(), self.refresh_models()),
        ).pack(fill="x", padx=8, pady=2)

    def _set_agent_prompt(self, prompt, agent_name=None):
        if agent_name and hasattr(self, "agent_name_var"):
            self.agent_name_var.set(agent_name)
            if hasattr(self, "agent_selector_button"):
                self.agent_selector_button.configure(text="%s  ▾" % agent_name)

        if hasattr(self, "agent_input"):
            self.agent_input.delete("1.0", tk.END)
            self.agent_input.insert("1.0", prompt)
            self.agent_input.configure(text_color="#f4f4f4")
            self.agent_input.focus_set()

    def _agent_input_focus(self, event=None):
        if not hasattr(self, "agent_input"):
            return

        value = self.agent_input.get("1.0", tk.END).strip()

        if value.startswith("Ask anything about ") or value == "Ask about the project…":
            self.agent_input.delete("1.0", tk.END)
        self.agent_input.configure(text_color="#f4f4f4")

    def _resize_agent_column(self, event=None):
        try:
            viewport_height = max(200, self.agent_chat_viewport.winfo_height())
            self.agent_chat_scroll.configure(height=viewport_height)
            width = max(
                300,
                min(
                    CHAT_MAX_WIDTH,
                    self.agent_chat_viewport.winfo_width() - CHAT_SIDE_PADDING * 2,
                ),
            )
            self.agent_chat_column.configure(width=width)
        except tk.TclError:
            pass

    def _resize_agent_input(self, event=None):
        if not hasattr(self, "agent_input"):
            return

        value = self.agent_input.get("1.0", tk.END)
        line_count = max(1, min(5, value.count("\n") + 1))
        height = 44 + (line_count - 1) * 20
        self.agent_input.configure(height=height)
        if hasattr(self, "agent_composer"):
            self.agent_composer.configure(height=62 + (line_count - 1) * 20)

    def _agent_enter(self, event):
        # Shift+Enter keeps a newline; Enter sends, like modern chat apps.
        if event.state & 0x1:
            return None

        self.send_agent_message()
        return "break"

    def refresh_models(self):
        def work():
            try:
                models = self.omlx.models()
                chosen = self.omlx.choose_models(
                    models
                )

                self._main_after(lambda: self._models_ready(models, chosen))

            except Exception as exc:
                self._main_after(lambda error=exc: self._models_failed(error))

        self._thread(work)

    def _models_ready(self, models, chosen):
        self.models = models
        groups = self.omlx.classify_models(models)
        self.chat_models = groups["chat"]
        self.embedding_models = groups["embedding"]
        self.reranker_models = groups["reranker"]

        if not self.chat_model or self.chat_model not in self.chat_models:
            self.chat_model = chosen["chat"]

        if (
            not self.embedding_model
            or self.embedding_model not in self.embedding_models
        ):
            self.embedding_model = chosen["embedding"]

        self.reranker_model = chosen["reranker"]
        self._refresh_agent_model_widgets()

    def _refresh_agent_model_widgets(self):
        chat_values = self.chat_models or ["No chat models"]
        embed_values = self.embedding_models or ["No embedding models"]

        # Forge owns the user-facing build model picker.
        if hasattr(self, "forge_model_menu"):
            try:
                self.forge_model_menu.configure(values=chat_values)
                if self.chat_model:
                    self.forge_model_var.set(self.chat_model)
            except tk.TclError:
                pass

        # Legacy Analyze-agent widgets remain unused, but keep this compatible for
        # old sessions/tests instead of deleting a large working code path at once.
        if not hasattr(self, "chat_model_menu"):
            return

        try:
            self.chat_model_menu.configure(values=chat_values)
            embed_menu = getattr(self, "embed_model_menu", None)

            if embed_menu is not None:
                embed_menu.configure(values=embed_values)

            if self.chat_model:
                self.chat_model_var.set(self.chat_model)

            if self.embedding_model and embed_menu is not None:
                self.embed_model_var.set(self.embedding_model)
        except tk.TclError:
            return

    def _models_failed(self, error):
        self._set_agent_brain_status("oMLX unavailable", RED)
        if self.mode == "build":
            self._set_forge_status("oMLX unavailable · start the server on port 8000", RED)

    def _chat_model_changed(self, value):
        self.chat_model = value

    def _embedding_model_changed(self, value):
        self.embedding_model = value
        self.index_ready = False

    def _force_reindex(self):
        self.index_ready = False
        self.index_state = "Refreshing Project Brain…"
        self.start_indexing()

    def _set_agent_runtime(self, text, color="#697482"):
        self.agent_notice = text

        try:
            if hasattr(self, "agent_runtime_status"):
                self.agent_runtime_status.configure(
                    text=text,
                    text_color=color,
                )
        except tk.TclError:
            pass

    def _start_agent_status(self):
        self.agent_request_token += 1
        token = self.agent_request_token
        self.agent_status_frame = 0
        self._set_agent_stage("Searching project…", YELLOW)

        def advance(stage, color):
            if token != self.agent_request_token:
                return
            self._set_agent_stage(stage, color)

        self.after(450, lambda: advance("Reading relevant code…", YELLOW))
        self.after(1000, lambda: advance("Thinking…", YELLOW))

    def _set_agent_stage(self, text, color):
        self.agent_status_frame = (self.agent_status_frame + 1) % 4
        dots = "." * self.agent_status_frame
        self._set_agent_runtime(text.rstrip("…") + dots, color)

    def _stop_agent_status(self, text, color):
        self.agent_request_token += 1
        self._set_agent_runtime(text, color)

    def _set_agent_brain_status(self, text, color):
        try:
            if hasattr(self, "agent_brain_status"):
                self.agent_brain_status.configure(
                    text=text,
                    text_color=color,
                )
        except tk.TclError:
            pass

    def send_agent_message(self):
        if not self.index_ready:
            self._set_agent_runtime(
                "Brain not ready",
                RED,
            )

            self._set_agent_brain_status("● Brain not ready", RED)
            return

        question = self.agent_input.get(
            "1.0",
            tk.END,
        ).strip()

        if not question or question.startswith("Ask anything about ") or question == "Ask about the project…":
            return

        self.agent_input.delete(
            "1.0",
            tk.END,
        )

        agent_name = self.agent_name_var.get()
        chat_model = self.chat_model_var.get()
        embed_model = self.embed_model_var.get()

        self.chat_model = chat_model
        self.embedding_model = embed_model
        self.pending_agent_name = agent_name

        self.agent_history.append({
            "role": "user",
            "content": question,
            "agent": agent_name,
        })

        self.agent_history.append({
            "role": "assistant",
            "content": "",
            "agent": agent_name,
            "streaming": True,
        })
        self.agent_streaming = True

        self._render_agent_history()
        self._poll_agent_stream()

        self.agent_send_button.configure(
            state="disabled",
            text="…",
        )

        self._start_agent_status()

        self.agent_request_started = time.perf_counter()
        prior_history = self.agent_history[:-1]
        depth = self.agent_depth_var.get() if hasattr(self, "agent_depth_var") else "Fast"

        def work():
            try:
                result = self.agent_service.answer(
                    self.project_key,
                    self.graph,
                    question,
                    agent_name,
                    chat_model,
                    embed_model,
                    selected_component=self.selected_component,
                    chat_history=prior_history,
                    depth=depth,
                    stream_callback=self._queue_agent_token,
                )

                self._main_after(lambda: self._agent_answer_ready(result))

            except Exception as exc:
                self._main_after(lambda error=exc: self._agent_answer_failed(error))

        self._thread(work)

    def _queue_agent_token(self, token):
        self.agent_stream_queue.put(token)

    def _poll_agent_stream(self):
        if not hasattr(self, "agent_chat_column"):
            return

        parts = []
        while True:
            try:
                parts.append(self.agent_stream_queue.get_nowait())
            except Empty:
                break

        if parts and self.agent_history:
            current = self.agent_history[-1]
            if current.get("role") == "assistant" and current.get("streaming"):
                self._set_agent_stage("Generating…", ACCENT)
                current["content"] += "".join(parts)
                self._render_agent_history()

        if self.agent_streaming or parts:
            try:
                self.after(90, self._poll_agent_stream)
            except tk.TclError:
                return

    def _agent_answer_ready(self, result):
        elapsed = max(
            0.0,
            time.perf_counter() - self.agent_request_started,
        )

        source_count = len(result.get("sources", []))
        context_k = result.get("context_chars", 0) / 1000.0
        mode = result.get("mode", "rag").upper()
        depth = result.get("depth", "Fast").upper()

        meta = "%s/%s · %d sources · %.1fk ctx · %.1fs" % (
            depth,
            mode,
            source_count,
            context_k,
            elapsed,
        )

        if self.agent_history and self.agent_history[-1].get("streaming"):
            answer_item = self.agent_history[-1]
            answer_item["content"] = result["answer"]
            answer_item["meta"] = meta
            answer_item.pop("streaming", None)
        else:
            self.agent_history.append({
                "role": "assistant",
                "content": result["answer"],
                "agent": getattr(
                    self,
                    "pending_agent_name",
                    self.agent_name_var.get(),
                ),
                "meta": meta,
            })

        self.last_agent_sources = result["sources"]
        self.agent_streaming = False

        try:
            user_question = next(
                (item.get("content", "") for item in reversed(self.agent_history[:-1]) if item.get("role") == "user"),
                "",
            )
            source_paths = ", ".join(item.get("path", "") for item in self.last_agent_sources[:4])
            memory_text = "Question: %s\nAnswer: %s" % (user_question[:1200], result["answer"][:5000])
            self._remember_project(
                "analysis_chat",
                "chat-%d" % int(time.time() * 1000),
                memory_text,
                path=source_paths,
            )
        except Exception:
            LOGGER.exception("Could not queue Analyze Agent memory")

        self._render_agent_history()
        self._render_agent_sources()

        try:
            self.agent_send_button.configure(
                state="normal",
                text="↑",
            )
        except tk.TclError:
            pass

        self._stop_agent_status("Ready", GREEN)

    def _agent_answer_failed(self, error):
        elapsed = max(
            0.0,
            time.perf_counter() - self.agent_request_started,
        )

        error_content = (
            "### Agent error\n"
            "I could not complete that request.\n\n"
            "`%s`" % str(error)
        )
        if self.agent_history and self.agent_history[-1].get("streaming"):
            answer_item = self.agent_history[-1]
            answer_item["content"] = error_content
            answer_item["meta"] = "failed · %.1fs" % elapsed
            answer_item.pop("streaming", None)
        else:
            self.agent_history.append({
                "role": "assistant",
                "content": error_content,
                "agent": getattr(
                    self,
                    "pending_agent_name",
                    self.agent_name_var.get(),
                ),
                "meta": "failed · %.1fs" % elapsed,
            })

        self.agent_streaming = False

        self._render_agent_history()

        try:
            self.agent_send_button.configure(
                state="normal",
                text="↑",
            )
        except tk.TclError:
            pass

        self._stop_agent_status("Error", RED)

    def _append_agent_system(self, text):
        # Status/errors belong in the chrome, not permanently in chat history.
        self._set_agent_runtime(text, RED)

    def _render_agent_history(self):
        if not hasattr(self, "agent_chat_scroll") or not hasattr(self, "agent_chat_column"):
            return

        for child in self.agent_chat_column.winfo_children():
            child.destroy()

        if not self.agent_history:
            welcome = ctk.CTkFrame(
                self.agent_chat_column,
                fg_color="transparent",
            )
            welcome.pack(fill="both", expand=True, pady=(110, 20))

            ctk.CTkLabel(
                welcome,
                text="What do you want to understand?",
                text_color=TEXT,
                font=(UI_FONT, 24, "bold"),
            ).pack(pady=(0, 7))

            ctk.CTkLabel(
                welcome,
                text="Ask anything about %s" % self.project_name,
                text_color=MUTED,
                font=(UI_FONT, 10),
            ).pack(pady=(0, 24))

            suggestions = (
                ("Explain the architecture", "Explain this project's architecture and main execution flow."),
                ("Where does execution start?", "Where does this project start, and what happens next?"),
                ("Explain the selected module", "Explain the currently selected Canvas module and its responsibilities."),
                ("How would I add a feature?", "Plan how to add a new feature to this project and identify the best integration points."),
            )

            for label, prompt in suggestions:
                ctk.CTkButton(
                    welcome,
                    text=label,
                    width=230,
                    height=42,
                    corner_radius=12,
                    fg_color=PANEL,
                    hover_color=PANEL_2,
                    text_color=SOFT,
                    font=(UI_FONT, 9),
                    command=lambda value=prompt: self._set_agent_prompt(value),
                ).pack(pady=3)
            return

        for item in self.agent_history:
            message = ChatMessage(
                self.agent_chat_column,
                item.get("role", "assistant"),
                item.get("content", ""),
                agent_name=item.get("agent", "SoftwareForge"),
                meta=item.get("meta", ""),
                sources=(
                    self.last_agent_sources
                    if item is self.agent_history[-1]
                    and item.get("role") == "assistant"
                    else None
                ),
                on_source=self._open_agent_source,
                on_more_sources=self._toggle_sources,
            )
            message.pack(fill="x", padx=3, pady=1)

        def scroll_bottom():
            try:
                self.agent_chat_scroll._parent_canvas.yview_moveto(1.0)
            except Exception:
                pass

        self.after(35, scroll_bottom)

    def _render_agent_sources(self):
        if not hasattr(self, "sources_floating_button"):
            return

        if not self.last_agent_sources:
            self.sources_floating_button.place_forget()
            self._close_sources()
            return

        self.sources_floating_button.configure(
            text="Sources %d  ▾" % len(self.last_agent_sources),
        )
        self.sources_floating_button.place(relx=1.0, x=-18, y=12, anchor="ne")
        self.sources_overlay.configure(
            width=min(380, max(340, int(self.agent_chat_viewport.winfo_width() * 0.38))),
            height=min(500, max(420, int(self.agent_chat_viewport.winfo_height() - 80))),
        )

        for child in self.sources_overlay.winfo_children():
            child.destroy()

        panel_header = ctk.CTkFrame(self.sources_overlay, fg_color="transparent")
        panel_header.pack(fill="x", padx=14, pady=(12, 7))
        ctk.CTkLabel(
            panel_header,
            text="Sources",
            text_color=TEXT,
            font=(UI_FONT, 12, "bold"),
        ).pack(side="left")
        ctk.CTkButton(
            panel_header,
            text="×",
            width=26,
            height=26,
            corner_radius=8,
            fg_color="transparent",
            hover_color=SURFACE_HOVER,
            text_color=MUTED,
            font=(UI_FONT, 15),
            command=self._close_sources,
        ).pack(side="right")

        ctk.CTkLabel(
            self.sources_overlay,
            text="Evidence used for the latest answer",
            text_color=MUTED,
            font=(UI_FONT, 8),
        ).pack(anchor="w", padx=14, pady=(0, 8))

        source_scroll = ctk.CTkScrollableFrame(
            self.sources_overlay,
            fg_color="transparent",
        )
        source_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        for index, source in enumerate(self.last_agent_sources, 1):
            path = source["path"]
            start_line = source.get("start_line", 1)
            end_line = source.get("end_line", start_line)

            ctk.CTkButton(
                source_scroll,
                text="[%d]  %s\nL%s–%s\n%s" % (
                    index,
                    PurePosixPath(path).name,
                    start_line,
                    end_line,
                    path,
                ),
                anchor="w",
                height=66,
                corner_radius=11,
                fg_color=SURFACE_SOFT,
                hover_color=SURFACE_HOVER,
                text_color=SOFT,
                font=(UI_FONT, 8),
                command=lambda item=source:
                    self._preview_agent_source(item),
            ).pack(fill="x", padx=2, pady=4)

        self.sources_overlay.lift()

    def _preview_agent_source(self, source):
        path = source.get("path", "")
        start_line = source.get("start_line", 1)
        cache_key = "analyze:%s" % path
        cached = self.source_cache.get(cache_key)

        if cached is not None:
            self._show_agent_source_preview(source, cached)
            return

        self._set_agent_runtime("Reading source…", ACCENT)

        def work():
            try:
                content = self.sandbox.read_source(path)
                self.source_cache[cache_key] = content
                self._main_after(lambda: self._show_agent_source_preview(source, content))
            except Exception as exc:
                self._main_after(
                    lambda error=exc: self._set_agent_runtime(
                        "Could not preview source: %s" % error,
                        RED,
                    )
                )

        self._thread(work)

    def _show_agent_source_preview(self, source, content):
        if not hasattr(self, "sources_overlay"):
            return

        for child in self.sources_overlay.winfo_children():
            child.destroy()

        path = source.get("path", "")
        start_line = source.get("start_line", 1)
        end_line = source.get("end_line", start_line)

        header = ctk.CTkFrame(self.sources_overlay, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(12, 5))
        ctk.CTkButton(
            header,
            text="‹ Sources",
            width=86,
            height=28,
            corner_radius=8,
            fg_color="transparent",
            hover_color=SURFACE_HOVER,
            text_color=SOFT,
            font=(UI_FONT, 9),
            command=self._render_agent_sources,
        ).pack(side="left")
        ctk.CTkButton(
            header,
            text="×",
            width=26,
            height=26,
            corner_radius=8,
            fg_color="transparent",
            hover_color=SURFACE_HOVER,
            text_color=MUTED,
            font=(UI_FONT, 15),
            command=self._close_sources,
        ).pack(side="right")

        ctk.CTkLabel(
            self.sources_overlay,
            text=PurePosixPath(path).name,
            text_color=TEXT,
            anchor="w",
            font=(UI_FONT, 11, "bold"),
        ).pack(fill="x", padx=14, pady=(3, 0))
        ctk.CTkLabel(
            self.sources_overlay,
            text="%s\nLines %s–%s" % (path, start_line, end_line),
            text_color=MUTED,
            anchor="w",
            justify="left",
            font=(UI_FONT, 8),
        ).pack(fill="x", padx=14, pady=(2, 8))

        preview = SyntaxCodeView(self.sources_overlay)
        preview.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        preview.show_source(path, content, focus_line=start_line)

        actions = ctk.CTkFrame(self.sources_overlay, fg_color="transparent")
        actions.pack(fill="x", padx=12, pady=(0, 10))
        ctk.CTkButton(
            actions,
            text="Open in Canvas",
            height=30,
            corner_radius=8,
            fg_color=ACCENT,
            hover_color="#e65d4d",
            text_color="#ffffff",
            font=(UI_FONT, 8, "bold"),
            command=lambda: self._open_agent_source(path, start_line),
        ).pack(side="left")

        self.sources_overlay.configure(
            width=min(560, max(340, int(self.agent_chat_viewport.winfo_width() * 0.38))),
            height=min(620, max(460, int(self.agent_chat_viewport.winfo_height() - 80))),
        )
        self.sources_overlay.place(relx=1.0, x=-18, y=52, anchor="ne")
        self.sources_overlay.lift()
        self._set_agent_runtime("Ready", GREEN)

    def _toggle_sources(self):
        if not self.last_agent_sources:
            return

        if self.sources_overlay.winfo_ismapped():
            self._close_sources()
            return

        self.sources_overlay.place(relx=1.0, x=-18, y=52, anchor="ne")
        self.sources_overlay.lift()

    def _close_sources(self):
        try:
            if hasattr(self, "sources_overlay"):
                self.sources_overlay.place_forget()
        except tk.TclError:
            pass

    def _sources_resized(self, event=None):
        try:
            if (
                hasattr(self, "sources_overlay")
                and self.sources_overlay.winfo_ismapped()
                and self.agent_chat_viewport.winfo_width() < 900
            ):
                self._close_sources()
        except tk.TclError:
            pass

    def _open_agent_source(self, path, line):
        parent = str(
            PurePosixPath(path).parent
        )

        self.canvas_path = (
            ""
            if parent == "."
            else parent
        )

        self.show(
            "Canvas"
        )

        self.after(
            180,
            lambda: self._open_canvas_source_drawer_at_line(path, line),
        )


if __name__ == "__main__":
    SoftwareForge().mainloop()
