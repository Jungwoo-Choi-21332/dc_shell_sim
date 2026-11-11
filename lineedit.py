# -*- coding: utf-8 -*-
"""
lineedit.py  -  bash 처럼 동작하는 한 줄 편집기 (Windows / Linux 공용, 표준 라이브러리만)

  Tab            자동완성 (후보가 여러 개면 공통 부분까지, 한 번 더 누르면 후보 목록)
  Up / Down      명령 기록
  Left / Right   커서 이동 (Ctrl+B / Ctrl+F)
  Home / End     줄 처음 / 끝 (Ctrl+A / Ctrl+E)
  Backspace/Del  지우기 (Ctrl+H / Ctrl+D)
  Ctrl+U / K / W 커서 앞 전부 / 커서 뒤 전부 / 앞 단어 지우기
  Ctrl+L         화면 지우기
  Ctrl+R         기록 검색 (reverse-i-search)
  Ctrl+C         입력 취소,  Ctrl+D (빈 줄)  종료
"""
import os
import re
import sys
import shutil
import unicodedata

IS_WIN = os.name == "nt"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[\x01\x02]")


def char_w(ch):
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    o = ord(ch)
    if o < 32 or o == 127:
        return 0
    return 1


def str_w(s):
    return sum(char_w(c) for c in ANSI.sub("", s))


def available():
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
    except Exception:
        return False
    if os.environ.get("SIM_LINEEDIT", "1") in ("0", "off", "false"):
        return False
    if IS_WIN:
        try:
            import msvcrt  # noqa: F401
            return True
        except ImportError:
            return False
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
        return True
    except ImportError:
        return False


# --------------------------------------------------------------------------
# 키 입력
# --------------------------------------------------------------------------
class _Keys:
    def __enter__(self):
        if not IS_WIN:
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            import codecs
            self.dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        return self

    def __exit__(self, *a):
        if not IS_WIN:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def _byte(self, timeout=None):
        import select
        if timeout is not None:
            r, _, _ = select.select([self.fd], [], [], timeout)
            if not r:
                return None
        b = os.read(self.fd, 1)
        return b if b else None

    def get(self):
        """반환: ('char', ch) 또는 ('key', name)"""
        if IS_WIN:
            import msvcrt
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                code = msvcrt.getwch()
                return ("key", {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home", "O": "end",
                                "S": "delete", "s": "wleft", "t": "wright", "I": "pgup", "Q": "pgdn"}.get(code, ""))
            return self._ctrl(ch)
        b = self._byte()
        if b is None:
            return ("key", "eof")
        if b == b"\x1b":
            n = self._byte(0.05)
            if n is None:
                return ("key", "esc")
            if n in (b"[", b"O"):
                seq = b""
                while True:
                    c = self._byte(0.05)
                    if c is None:
                        break
                    seq += c
                    if c.isalpha() or c == b"~":
                        break
                m = {b"A": "up", b"B": "down", b"C": "right", b"D": "left", b"H": "home", b"F": "end",
                     b"1~": "home", b"4~": "end", b"7~": "home", b"8~": "end", b"3~": "delete",
                     b"1;5C": "wright", b"1;5D": "wleft", b"5~": "pgup", b"6~": "pgdn"}
                return ("key", m.get(seq, ""))
            if n in (b"b", b"B"):
                return ("key", "wleft")
            if n in (b"f", b"F"):
                return ("key", "wright")
            return ("key", "esc")
        s = self.dec.decode(b)
        while not s:
            b = self._byte()
            if b is None:
                return ("key", "eof")
            s = self.dec.decode(b)
        return self._ctrl(s)

    @staticmethod
    def _ctrl(ch):
        m = {"\r": "enter", "\n": "enter", "\t": "tab", "\x08": "bs", "\x7f": "bs", "\x03": "ctrl-c",
             "\x04": "ctrl-d", "\x01": "home", "\x05": "end", "\x02": "left", "\x06": "right", "\x0b": "ctrl-k",
             "\x15": "ctrl-u", "\x17": "ctrl-w", "\x0c": "ctrl-l", "\x12": "ctrl-r", "\x10": "up", "\x0e": "down",
             "\x1b": "esc", "\x07": "ctrl-g", "\x1a": "ctrl-z"}
        if ch in m:
            return ("key", m[ch])
        if ord(ch[0]) < 32:
            return ("key", "")
        return ("char", ch)


# --------------------------------------------------------------------------
# 편집기
# --------------------------------------------------------------------------
def _out(s):
    sys.stdout.write(s)
    sys.stdout.flush()


def _cols():
    return max(20, shutil.get_terminal_size((100, 30)).columns)


def common_prefix(items):
    if not items:
        return ""
    s1, s2 = min(items), max(items)
    for i, c in enumerate(s1):
        if c != s2[i]:
            return s1[:i]
    return s1


WORD_BREAK = " \t\n;|&<>{}[]\"'="


class LineEditor:
    def __init__(self, prompt, completer=None, history=None):
        self.prompt = prompt
        self.pw = str_w(prompt)
        self.completer = completer
        self.history = history if history is not None else []
        self.buf = []
        self.pos = 0
        self.rows_up = 0
        self.last_tab = False

    # ---- 화면 갱신 ----
    def refresh(self, prompt=None):
        if prompt is not None:
            self.prompt = prompt
            self.pw = str_w(prompt)
        cols = _cols()
        text = "".join(self.buf)
        total = self.pw + str_w(text)
        curw = self.pw + str_w("".join(self.buf[:self.pos]))
        s = ""
        if self.rows_up > 0:
            s += "\x1b[%dA" % self.rows_up
        s += "\r\x1b[J" + self.prompt + text
        end_row = total // cols
        if total > 0 and total % cols == 0:
            s += "\r\n"
        trow, tcol = curw // cols, curw % cols
        if end_row - trow > 0:
            s += "\x1b[%dA" % (end_row - trow)
        s += "\r"
        if tcol:
            s += "\x1b[%dC" % tcol
        self.rows_up = trow
        _out(s)

    def move_to_end(self):
        cols = _cols()
        total = self.pw + str_w("".join(self.buf))
        end_row = total // cols
        down = end_row - self.rows_up
        _out(("\x1b[%dB" % down if down > 0 else "") + "\r\n")
        self.rows_up = 0

    # ---- 자동완성 ----
    def word_bounds(self):
        i = self.pos
        q = None
        start = 0
        k = 0
        while k < i:
            c = self.buf[k]
            if q:
                if c == q:
                    q = None
            elif c in "\"'":
                q = c
                start = k + 1
            elif c in WORD_BREAK:
                start = k + 1
            k += 1
        return start, i

    def complete(self):
        if not self.completer:
            return
        b, e = self.word_bounds()
        line = "".join(self.buf)
        word = line[b:e]
        try:
            cands = self.completer(line, b, e, word) or []
        except Exception:
            cands = []
        cands = sorted(set(cands))
        if not cands:
            _out("\a")
            self.last_tab = False
            return
        if len(cands) == 1:
            c = cands[0]
            add = c if c.endswith("/") else c + " "
            self.buf[b:e] = list(add)
            self.pos = b + len(add)
            self.refresh()
            self.last_tab = False
            return
        pre = common_prefix(cands)
        if len(pre) > len(word):
            self.buf[b:e] = list(pre)
            self.pos = b + len(pre)
            self.refresh()
            self.last_tab = True
            return
        if not self.last_tab:
            _out("\a")
            self.last_tab = True
            return
        self.show_candidates(cands, word)
        self.last_tab = False

    def show_candidates(self, cands, word):
        self.move_to_end()
        disp = []
        for c in cands:
            d = c.rstrip("/")
            d = d.rsplit("/", 1)[-1] + ("/" if c.endswith("/") else "")
            disp.append(d)
        if len(disp) > 100:
            _out("Display all %d possibilities? (y or n)" % len(disp))
            with _Keys() as k:
                kind, v = k.get()
            _out("\r\n")
            if not (kind == "char" and v.lower() == "y"):
                self.refresh()
                return
        cols = _cols()
        w = max(str_w(d) for d in disp) + 2
        per = max(1, cols // w)
        rows = (len(disp) + per - 1) // per
        lines = []
        for r in range(rows):
            parts = []
            for c in range(per):
                k = c * rows + r
                if k < len(disp):
                    d = disp[k]
                    parts.append(d + " " * (w - str_w(d)))
            lines.append("".join(parts).rstrip())
        _out("\r\n".join(lines) + "\r\n")
        self.rows_up = 0
        self.refresh()

    # ---- 기록 검색 ----
    def reverse_search(self, keys):
        q = ""
        idx = len(self.history)
        match = ""
        saved = (self.prompt, self.pw)

        def show():
            p = "(reverse-i-search)`%s': " % q
            self.buf = list(match)
            self.pos = len(self.buf)
            self.refresh(p)
        show()
        while True:
            kind, v = keys.get()
            if kind == "char":
                q += v
            elif v == "bs":
                q = q[:-1]
                idx = len(self.history)
            elif v == "ctrl-r":
                idx = idx - 1 if idx > 0 else idx
            elif v in ("ctrl-c", "ctrl-g", "esc"):
                match = ""
                break
            else:
                self.prompt, self.pw = saved
                self.refresh()
                return v
            for k in range(min(idx, len(self.history)) - 1, -1, -1):
                if q and q in self.history[k]:
                    match = self.history[k]
                    idx = k + (0 if v == "ctrl-r" else 1)
                    break
            show()
        self.prompt, self.pw = saved
        self.buf = list(match)
        self.pos = len(self.buf)
        self.refresh()
        return None

    # ---- 메인 ----
    def read(self):
        hidx = len(self.history)
        draft = None
        _out(self.prompt)
        with _Keys() as keys:
            pending = None
            while True:
                if pending:
                    kind, v = "key", pending
                    pending = None
                else:
                    kind, v = keys.get()
                if not (kind == "key" and v == "tab"):
                    self.last_tab = False
                if kind == "char":
                    self.buf[self.pos:self.pos] = list(v)
                    self.pos += len(v)
                    if self.pos == len(self.buf) and str_w("".join(self.buf)) + self.pw < _cols() - 1 \
                            and "\n" not in v:
                        _out(v)
                    else:
                        self.refresh()
                    continue
                if v == "enter":
                    self.pos = len(self.buf)
                    self.refresh()
                    self.move_to_end()
                    return "".join(self.buf)
                if v == "tab":
                    self.complete()
                elif v == "bs":
                    if self.pos > 0:
                        del self.buf[self.pos - 1]
                        self.pos -= 1
                        self.refresh()
                elif v == "delete":
                    if self.pos < len(self.buf):
                        del self.buf[self.pos]
                        self.refresh()
                elif v == "ctrl-d":
                    if not self.buf:
                        _out("\r\n")
                        raise EOFError()
                    if self.pos < len(self.buf):
                        del self.buf[self.pos]
                        self.refresh()
                elif v == "eof":
                    raise EOFError()
                elif v == "ctrl-c":
                    self.pos = len(self.buf)
                    self.refresh()
                    _out("^C")
                    self.move_to_end()
                    raise KeyboardInterrupt()
                elif v == "left":
                    if self.pos > 0:
                        self.pos -= 1
                        self.refresh()
                elif v == "right":
                    if self.pos < len(self.buf):
                        self.pos += 1
                        self.refresh()
                elif v == "home":
                    self.pos = 0
                    self.refresh()
                elif v == "end":
                    self.pos = len(self.buf)
                    self.refresh()
                elif v == "wleft":
                    p = self.pos
                    while p > 0 and self.buf[p - 1] == " ":
                        p -= 1
                    while p > 0 and self.buf[p - 1] not in " /":
                        p -= 1
                    self.pos = p
                    self.refresh()
                elif v == "wright":
                    p = self.pos
                    while p < len(self.buf) and self.buf[p] == " ":
                        p += 1
                    while p < len(self.buf) and self.buf[p] not in " /":
                        p += 1
                    self.pos = p
                    self.refresh()
                elif v == "ctrl-u":
                    del self.buf[:self.pos]
                    self.pos = 0
                    self.refresh()
                elif v == "ctrl-k":
                    del self.buf[self.pos:]
                    self.refresh()
                elif v == "ctrl-w":
                    p = self.pos
                    while p > 0 and self.buf[p - 1] == " ":
                        p -= 1
                    while p > 0 and self.buf[p - 1] != " ":
                        p -= 1
                    del self.buf[p:self.pos]
                    self.pos = p
                    self.refresh()
                elif v == "ctrl-l":
                    _out("\x1b[2J\x1b[H")
                    self.rows_up = 0
                    self.refresh()
                elif v in ("up", "down"):
                    if not self.history:
                        continue
                    if draft is None:
                        draft = "".join(self.buf)
                    if v == "up" and hidx > 0:
                        hidx -= 1
                    elif v == "down" and hidx < len(self.history):
                        hidx += 1
                    else:
                        continue
                    text = self.history[hidx] if hidx < len(self.history) else draft
                    self.buf = list(text)
                    self.pos = len(self.buf)
                    self.refresh()
                elif v == "ctrl-r":
                    nxt = self.reverse_search(keys)
                    if nxt:
                        pending = nxt


def read_line(prompt, completer=None, history=None):
    """tty 이면 편집기, 아니면 input()"""
    if not available():
        return input(prompt)
    return LineEditor(prompt, completer, history).read()
