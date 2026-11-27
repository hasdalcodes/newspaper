"""
Arabic / right-to-left text for Tk widgets that cannot do it themselves.

Tk on X11 (Linux, FreeBSD) draws characters left to right, one by one: Arabic comes out with isolated
letters in reversed order. Windows and macOS Tk use the operating system's text engine and are fine.

The fix is to hand Tk a *visual* string: letters already replaced by their joined presentation forms
(shaping) and the characters already in left-to-right display order (bidi reordering). Because Tk would
wrap such a string at the wrong end, lines have to be broken here, on the logical text, and every line
converted on its own (`wrap`).

Pure Python, standard library only. Shaping tables are built from the Unicode database at import time.
"""
import re
import unicodedata as ud
from functools import lru_cache

# --------------------------------------------------------------------------- shaping tables
_LAM = "\u0644"
_ALEFS = {"\u0622", "\u0623", "\u0625", "\u0627"}
_FORMS: dict[str, dict[str, str]] = {}                  # base letter -> {isolated, final, initial, medial}
_LIGA: dict[tuple[str, str], dict[str, str]] = {}       # (lam, alef) -> {isolated, final}


def _build_tables() -> None:
    for cp in (*range(0xFB50, 0xFE00), *range(0xFE70, 0xFEFD)):
        ch = chr(cp)
        dec = ud.decomposition(ch)
        if not dec.startswith("<"):
            continue
        tag, *rest = dec.split()
        tag = tag.strip("<>")
        if tag not in ("isolated", "final", "initial", "medial"):
            continue
        base = tuple(chr(int(h, 16)) for h in rest)
        if len(base) == 1:
            _FORMS.setdefault(base[0], {})[tag] = ch
        elif len(base) == 2 and base[0] == _LAM and base[1] in _ALEFS:
            _LIGA.setdefault(base, {})[tag] = ch


_build_tables()

_RTL_RE = re.compile("[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufeff]")
_ARABIC_RE = re.compile("[\u0600-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")
_STRIP = dict.fromkeys([0x200C, 0x200D, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)])
_MIRROR = {a: b for pair in ("()", "[]", "{}", "<>", "«»", "‹›") for a, b in (pair, pair[::-1])}


def has_rtl(text: str) -> bool:
    """True if the text contains Hebrew/Arabic-script characters (so it needs special handling)."""
    return bool(text) and _RTL_RE.search(text) is not None


def _join_type(c: str) -> str:
    """D dual-joining, R right-joining, T transparent (marks), N non-joining."""
    if c == "\u0640" or c == "\u200d":      # tatweel, zero-width joiner
        return "D"
    f = _FORMS.get(c)
    if f is None:
        return "T" if ud.category(c) == "Mn" else "N"
    if "initial" in f:
        return "D"
    return "R" if "final" in f else "N"


def shape(text: str) -> str:
    """Replace Arabic letters by the presentation form that matches their position in the word.
    The result is still in logical order."""
    if not text or _ARABIC_RE.search(text) is None:
        return text
    toks: list[tuple[str, object]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == _LAM and i + 1 < n and (c, text[i + 1]) in _LIGA:
            toks.append(("lig", (c, text[i + 1])))
            i += 2
        else:
            toks.append(("ch", c))
            i += 1
    jts = ["R" if k == "lig" else _join_type(v) for k, v in toks]  # type: ignore[arg-type]
    out = []
    last = len(toks)
    for k, (kind, val) in enumerate(toks):
        j = jts[k]
        if j == "T" or (kind == "ch" and val not in _FORMS):
            out.append(val)
            continue
        p = k - 1
        while p >= 0 and jts[p] == "T":
            p -= 1
        q = k + 1
        while q < last and jts[q] == "T":
            q += 1
        joins_prev = p >= 0 and jts[p] == "D" and j in ("D", "R")
        joins_next = q < last and j == "D" and jts[q] in ("D", "R")
        form = ("medial" if joins_prev and joins_next else "final" if joins_prev
                else "initial" if joins_next else "isolated")
        table = _LIGA[val] if kind == "lig" else _FORMS[val]  # type: ignore[index]
        out.append(table.get(form) or table.get("isolated") or (val if kind == "ch" else "".join(val)))  # type: ignore[arg-type]
    return "".join(out)


# --------------------------------------------------------------------------- bidi (simplified UBA)
def _reorder(s: str, rtl: bool) -> str:
    """Unicode bidirectional algorithm for one line without explicit embeddings: weak/neutral resolution,
    implicit levels, line reordering (L2), mirroring (L4) and re-attaching combining marks."""
    n = len(s)
    if not n:
        return s
    sos = "R" if rtl else "L"
    t = [ud.bidirectional(c) or "ON" for c in s]
    prev = sos
    for i in range(n):                                   # W1
        if t[i] == "NSM":
            t[i] = prev
        prev = t[i]
    strong = sos
    for i in range(n):                                   # W2
        if t[i] in ("L", "R", "AL"):
            strong = t[i]
        elif t[i] == "EN" and strong == "AL":
            t[i] = "AN"
    t = ["R" if x == "AL" else x for x in t]             # W3
    for i in range(1, n - 1):                            # W4
        if t[i] == "ES" and t[i - 1] == "EN" and t[i + 1] == "EN":
            t[i] = "EN"
        elif t[i] == "CS" and t[i - 1] == t[i + 1] and t[i - 1] in ("EN", "AN"):
            t[i] = t[i - 1]
    i = 0
    while i < n:                                         # W5
        if t[i] == "ET":
            j = i
            while j < n and t[j] == "ET":
                j += 1
            if (i > 0 and t[i - 1] == "EN") or (j < n and t[j] == "EN"):
                t[i:j] = ["EN"] * (j - i)
            i = j
        else:
            i += 1
    t = ["ON" if x in ("ES", "ET", "CS") else x for x in t]  # W6
    strong = sos
    for i in range(n):                                   # W7
        if t[i] in ("L", "R"):
            strong = t[i]
        elif t[i] == "EN" and strong == "L":
            t[i] = "L"

    def side(x: str) -> str:
        return "R" if x in ("R", "EN", "AN") else "L"
    i = 0
    while i < n:                                         # N1, N2
        if t[i] not in ("L", "R", "EN", "AN"):
            j = i
            while j < n and t[j] not in ("L", "R", "EN", "AN"):
                j += 1
            before = side(t[i - 1]) if i > 0 else sos
            after = side(t[j]) if j < n else sos
            t[i:j] = [before if before == after else sos] * (j - i)
            i = j
        else:
            i += 1

    base = 1 if rtl else 0
    lv = [(0 if x == "L" else 1 if x == "R" else 2) if base == 0 else (1 if x == "R" else 2) for x in t]  # I1, I2
    idx = list(range(n))
    top = max(lv)
    low = min((x for x in lv if x % 2), default=top + 1)
    for level in range(top, low - 1, -1):               # L2
        i = 0
        while i < n:
            if lv[idx[i]] >= level:
                j = i
                while j < n and lv[idx[j]] >= level:
                    j += 1
                idx[i:j] = idx[i:j][::-1]
                i = j
            else:
                i += 1
    chars = [(_MIRROR.get(c, c) if lv[k] % 2 else c) for k, c in enumerate(s)]  # L4
    vis = [chars[k] for k in idx]
    vlv = [lv[k] for k in idx]
    res, i, m = [], 0, len(vis)                          # marks must follow their base in the drawn string
    while i < m:
        if vlv[i] % 2 and ud.category(vis[i]) == "Mn":
            j = i
            while j < m and vlv[j] % 2 and ud.category(vis[j]) == "Mn":
                j += 1
            if j < m:
                res.append(vis[j])
                res.extend(reversed(vis[i:j]))
                i = j + 1
                continue
        res.append(vis[i])
        i += 1
    return "".join(res)


def to_visual(text: str, rtl: bool) -> str:
    """One line of logical text -> the string to give Tk (joined letters, left-to-right display order).
    `rtl` is the paragraph direction."""
    if not rtl and not has_rtl(text):
        return text
    return _reorder(shape(text), rtl).translate(_STRIP)


@lru_cache(maxsize=20000)
def _glyphs(word: str) -> str:
    """The characters Tk will draw for one word (shaped, invisible controls removed). Order does not
    matter for its width, so the bidi step is skipped."""
    return shape(word).translate(_STRIP) if has_rtl(word) else word


def cached_measure(measure, size: int = 20000):
    """Wrap a width function (e.g. tkinter Font.measure, a round trip into Tk) with a memo.
    Words repeat a lot across news cards, so most lookups never reach Tk."""
    memo: dict[str, int] = {}

    def m(s: str) -> int:
        w = memo.get(s)
        if w is None:
            if len(memo) >= size:
                memo.clear()
            w = memo[s] = measure(s)
        return w
    return m


def wrap(text: str, measure, max_px: int, rtl: bool) -> str:
    """Break logical text into lines that fit `max_px` (measured on the display string with `measure`),
    convert each line to display order and join them with newlines. Existing newlines are kept.

    Shaping never joins across a space and reordering does not change widths, so a line's width is the
    sum of its words' display widths plus the spaces: every word is shaped and measured once, and each
    finished line is converted once (linear, instead of re-converting the growing line for every word)."""
    space = measure(" ")
    lines = []
    for para in text.split("\n"):
        cur: list[str] = []
        cur_w = 0
        for word in para.split(" "):
            w = measure(_glyphs(word)) if word else 0
            if cur and cur_w + space + w > max_px:
                lines.append(to_visual(" ".join(cur), rtl))
                cur, cur_w = [word], w
            else:
                cur_w = cur_w + space + w if cur else w
                cur.append(word)
        lines.append(to_visual(" ".join(cur), rtl))
    return "\n".join(lines)
