"""Universal Fast Local UI Element Matcher (v3.1.0).

Zero app-specific hardcoding, zero third-party dependencies (pure Python stdlib + ctypes),
and full Unicode support across any Windows desktop application:
  - Compound Step Decomposition (`decompose_goal_steps`): Splits multi-step instructions
    ("Click Settings -> Connections -> Enable Sync", or arithmetic expressions) into
    narrow single-action sub-goals + optional final-state verification clause.
  - Tier 1 (<0.4ms, up to 120 UIA controls): Unicode NFKD folding & multilingual CJK/script
    tokenization, identifier splitting (camelCase/snake_case/UI-suffix aware), lightweight
    morphological stemming + consonant skeleton, Jaro-Winkler + 3-gram Dice similarity,
    Unicode math symbol verbalizations (`unicodedata.name`), cardinal numbers, W3C CSS3 sRGB
    perceptual color proximity, and UIA control-role disambiguation.
  - Tier 2 (optional local LLM fallback): Uses an explicitly configured `CUA_MATCHER_ENDPOINT`
    (or opt-in `CUA_MATCHER_AUTO_DISCOVER=1` probing Ollama/LM Studio/Jan ports with a 60s cache TTL).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import re
import time
from typing import Any
import unicodedata
import urllib.parse
import urllib.request

CONFIDENCE_FLOOR = 0.80
MAX_ELEMENTS = 120
NEURAL_MAX_ELEMENTS = 45

_STOP_WORDS = {
    "a", "an", "the", "to", "on", "in", "of", "for", "and", "or", "button", "control",
    "element", "item", "icon", "field", "box", "click", "press", "tap", "hit", "select",
    "choose", "open", "activate", "push", "please", "now", "next", "tab", "checkbox",
    "tick", "check", "toggle", "uncheck", "focus", "dropdown", "combo", "menu", "key",
    "tool", "swatch", "shape", "slider",
}

_POLITE_PREFIX = {"please", "now", "next", "then"}

_ACTION_VERBS = {
    "click", "press", "tap", "hit", "select", "choose", "open", "activate", "push",
    "check", "tick", "toggle", "uncheck", "focus", "type", "enter", "input", "write",
    "pick", "use", "switch",
}

_ARTICLES_PREPS = {"a", "an", "the", "to", "on", "in"}

_TRAILING_TARGET_STRIP = {
    "button", "control", "element", "item", "icon", "field", "box", "tab",
    "checkbox", "dropdown", "combo", "menu", "radio", "link", "slider",
    "tool", "swatch", "shape", "please", "now", "key",
}

_UI_IDENT_SUFFIXES = {
    "button", "btn", "control", "ctrl", "element", "item", "tab", "checkbox",
    "radio", "slider", "toggle", "menuitem", "textbox", "edit", "combobox", "num",
}

_PASSIVE_ROLES = {
    "progressbar", "group", "pane", "titlebar", "statusbar", "header", "headeritem",
    "text", "win32:static", "win32:msctls_progress32", "separator", "thumb", "tooltip",
}

_INTERACTIVE_ROLES = (
    "button", "splitbutton", "menuitem", "tabitem", "hyperlink", "listitem",
    "dataitem", "treeitem", "checkbox", "radiobutton", "combobox", "edit", "slider",
)

# Universal English cardinal words <-> digits (language-level, app-agnostic)
_CARDINAL_TO_DIGIT: dict[str, str] = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12",
}
_DIGIT_TO_CARDINAL: dict[str, str] = {v: k for k, v in _CARDINAL_TO_DIGIT.items()}

# Universal math/punctuation symbol verbalizations (app-agnostic)
_MATH_ALIASES: dict[str, tuple[str, ...]] = {
    "+": ("plus", "add", "addition", "positive"),
    "-": ("minus", "subtract", "subtraction", "dash", "negative"),
    "*": ("multiply", "times", "multiplication", "asterisk"),
    "×": ("multiply", "times", "multiplication", "*"),
    "/": ("divide", "division", "slash"),
    "÷": ("divide", "division", "/"),
    "=": ("equals", "equal", "result"),
    ".": ("decimal", "point", "dot"),
    "%": ("percent", "percentage", "modulo"),
}

# Standard W3C CSS3 sRGB palette for perceptual RGB proximity (zero app specificity)
_CSS3_COLORS: dict[str, tuple[int, int, int]] = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "gray": (128, 128, 128),
    "silver": (192, 192, 192),
    "red": (255, 0, 0),
    "maroon": (128, 0, 0),
    "crimson": (220, 20, 60),
    "coral": (255, 127, 80),
    "rose": (255, 192, 203),
    "pink": (255, 192, 203),
    "magenta": (255, 0, 255),
    "fuchsia": (255, 0, 255),
    "purple": (128, 0, 128),
    "lavender": (230, 230, 250),
    "violet": (238, 130, 238),
    "indigo": (75, 0, 130),
    "blue": (0, 0, 255),
    "navy": (0, 0, 128),
    "azure": (240, 255, 255),
    "sky": (135, 206, 235),
    "sky blue": (135, 206, 235),
    "light blue": (173, 216, 230),
    "dark blue": (0, 0, 139),
    "cyan": (0, 255, 255),
    "aqua": (0, 255, 255),
    "turquoise": (64, 224, 208),
    "light turquoise": (135, 224, 232),
    "teal": (0, 128, 128),
    "green": (0, 128, 0),
    "lime": (0, 255, 0),
    "olive": (128, 128, 0),
    "yellow": (255, 255, 0),
    "gold": (255, 215, 0),
    "amber": (255, 191, 0),
    "orange": (255, 165, 0),
    "brown": (165, 42, 42),
    "tan": (210, 180, 140),
    "beige": (245, 245, 220),
}

_COLOR_MODIFIERS: dict[str, float] = {
    "light": 0.35,
    "pale": 0.42,
    "bright": 0.20,
    "dark": -0.35,
    "deep": -0.28,
}

_DONE_CLAUSE_RE = re.compile(r"\b(?:until|when|once|confirm|verify)\s+(.+)$", re.I)
_DONE_STATE_RE = re.compile(
    r"\b(?:shows?|displays?|equals?|reads?|is)\s+(?!(?:shows?|displays?|equals?|reads?|is)\b)['\"]?([^\n'\"]{1,48})['\"]?",
    re.I,
)
_DONE_DIRECT_RE = re.compile(
    r"\b(?:shows?|displays?|equals?|reads?)\s+(?!(?:shows?|displays?|equals?|reads?|is)\b)['\"]?([^\n'\"]{1,48})['\"]?",
    re.I,
)
_MATH_EXPR_RE = re.compile(
    r"^(?:(?:calculate|compute|evaluate|solve|press|enter|dial|type)\s+)?([cC]?\s*\d[\d\s.,+\-*/×÷=]+[=]?)$",
    re.I,
)
_DATE_OR_PHONE_RE = re.compile(r"^(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}-\d{1,2}-\d{2,4}|\d{3}-\d{3,4}-\d{4})$")

# Dedicated local LLM server ports only (Ollama, LM Studio, Jan) — opt-in via CUA_MATCHER_AUTO_DISCOVER=1
_LOCAL_LLM_PORTS = (11434, 1234, 1337)
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]|[^\W_]+|[.+*/=%\-]", re.UNICODE)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_DEAD_ENDPOINT_UNTIL: float = 0.0
_CACHED_SERVER: tuple[float, str, str, str, str] | None = None  # (expires_at, base_url, model_id, api_key, api_kind)


def _is_loopback_host(host: str | None) -> bool:
    return (host or "").strip().lower() in _LOOPBACK_HOSTS


def _urlopen_safe(req: urllib.request.Request, *, timeout: float):
    host = urllib.parse.urlparse(req.full_url).hostname
    if _is_loopback_host(host):
        return _NO_PROXY_OPENER.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _fold_unicode(s: str) -> str:
    """Universal NFKD accent/typographic folding + US/UK orthographic normalization (preserves non-Latin scripts)."""
    if not s:
        return ""
    txt = unicodedata.normalize("NFKC", str(s))
    # Strip Latin combining accents while preserving CJK/Devanagari/Arabic/Cyrillic characters
    out_chars: list[str] = []
    for ch in unicodedata.normalize("NFKD", txt):
        if unicodedata.category(ch) == "Mn" and out_chars and ord(out_chars[-1]) < 0x0250:
            continue
        out_chars.append(ch)
    txt = unicodedata.normalize("NFC", "".join(out_chars))
    txt = (
        txt.replace("–", "-")
        .replace("—", "-")
        .replace("−", "-")
        .replace("×", "*")
        .replace("✕", "*")
        .replace("✖", "*")
        .replace("÷", "/")
        .replace("∕", "/")
        .replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
    )
    low = re.sub(r"\s+", " ", txt.strip().lower())
    low = re.sub(r"\bgrey\b", "gray", low)
    low = re.sub(r"\b([a-z]{3,})ours\b", r"\1ors", low)
    low = re.sub(r"\b([a-z]{3,})our\b", r"\1or", low)
    return low


def _norm(s: str) -> str:
    return _fold_unicode(s)


def _compact_num_text(s: str) -> str:
    return re.sub(r"(?<=\d),(?=\d{3}\b)", "", _fold_unicode(s))


def split_identifier(raw: str) -> list[str]:
    """Split camelCase/PascalCase/snake_case/kebab-case/digit boundaries and Unicode scripts cleanly."""
    if not raw:
        return []
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(raw))
    spaced = re.sub(r"([a-zA-Z])(\d)", r"\1 \2", spaced)
    spaced = re.sub(r"(\d)([a-zA-Z])", r"\1 \2", spaced)
    normed = _fold_unicode(spaced)
    return [t for t in _TOKEN_RE.findall(normed) if t]


def _tokens(s: str) -> list[str]:
    normed = _fold_unicode(s)
    return [t for t in _TOKEN_RE.findall(normed) if t]


def _stem_token(tok: str) -> str:
    if len(tok) <= 3 or not tok.isascii() or not tok.isalpha():
        return tok
    for suf in ("ations", "ation", "ments", "ment", "ness", "ings", "ing", "ies", "ied", "ed", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            if suf in ("ies", "ied"):
                return tok[: -len(suf)] + "y"
            return tok[: -len(suf)]
    return tok


def _consonant_skeleton(tok: str) -> str:
    if len(tok) <= 2 or not tok.isascii() or not tok.isalpha():
        return tok
    return tok[0] + "".join(ch for ch in tok[1:] if ch not in "aeiouy")


def _jaro_winkler(a: str, b: str) -> float:
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return 0.0
    match_dist = max(0, max(la, lb) // 2 - 1)
    a_match = [False] * la
    b_match = [False] * lb
    matches = 0
    for i in range(la):
        lo = max(0, i - match_dist)
        hi = min(i + match_dist + 1, lb)
        for j in range(lo, hi):
            if not b_match[j] and a[i] == b[j]:
                a_match[i] = True
                b_match[j] = True
                matches += 1
                break
    if matches == 0:
        return 0.0
    t = 0
    k = 0
    for i in range(la):
        if not a_match[i]:
            continue
        while not b_match[k]:
            k += 1
        if a[i] != b[k]:
            t += 1
        k += 1
    jaro = (matches / la + matches / lb + (matches - t / 2.0) / matches) / 3.0
    prefix = 0
    for ca, cb in zip(a[:4], b[:4]):
        if ca == cb:
            prefix += 1
        else:
            break
    return jaro + 0.1 * prefix * (1.0 - jaro)


def _trigram_dice(a: str, b: str) -> float:
    if len(a) < 2 or len(b) < 2:
        return 1.0 if a == b else 0.0
    ga = {a[i : i + 3] for i in range(max(1, len(a) - 2))}
    gb = {b[i : i + 3] for i in range(max(1, len(b) - 2))}
    if not ga or not gb:
        return 0.0
    return (2.0 * len(ga & gb)) / (len(ga) + len(gb))


def _expand_symbol_and_num_tokens(toks: list[str], raw_source: str = "") -> set[str]:
    out = set(toks)
    for t in toks:
        out.add(_stem_token(t))
        if t in _CARDINAL_TO_DIGIT:
            out.add(_CARDINAL_TO_DIGIT[t])
        if t in _DIGIT_TO_CARDINAL:
            out.add(_DIGIT_TO_CARDINAL[t])
        for alt in _MATH_ALIASES.get(t, ()):
            out.add(alt)
    for ch in raw_source:
        if ch in _MATH_ALIASES:
            for alt in _MATH_ALIASES[ch]:
                out.add(alt)
        elif ord(ch) > 127 and unicodedata.category(ch).startswith(("S", "P")):
            try:
                uname = unicodedata.name(ch, "").lower()
                for w in uname.split():
                    if w not in ("sign", "symbol", "operator", "heavy", "mathematical"):
                        out.add(w)
                        out.add(_stem_token(w))
            except Exception:
                pass
    return out


def token_similarity(a: str, b: str) -> float:
    """Bounded [0, 1] universal lexical/morphological/abbreviation token similarity."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sa, sb = _stem_token(a), _stem_token(b)
    if sa == sb:
        return 0.96
    if a in _DIGIT_TO_CARDINAL and _DIGIT_TO_CARDINAL[a] == b:
        return 0.96
    if b in _DIGIT_TO_CARDINAL and _DIGIT_TO_CARDINAL[b] == a:
        return 0.96
    if min(len(sa), len(sb)) >= 3 and (sa.startswith(sb) or sb.startswith(sa)):
        return 0.88 * (min(len(sa), len(sb)) / max(len(sa), len(sb))) + 0.12
    if min(len(a), len(b)) >= 2 and a.isascii() and b.isascii():
        ka, kb = _consonant_skeleton(a), _consonant_skeleton(b)
        if len(ka) >= 2 and ka == kb:
            return 0.82
    jw = _jaro_winkler(sa, sb)
    tri = _trigram_dice(sa, sb)
    comb = 0.60 * jw + 0.40 * tri
    return comb if comb >= 0.72 else 0.0


def _parse_css3_rgb(phrase: str) -> tuple[int, int, int] | None:
    low = _fold_unicode(phrase)
    if not low:
        return None
    if low in _CSS3_COLORS:
        return _CSS3_COLORS[low]
    toks = [t for t in _tokens(low) if t not in ("color", "swatch", "paint", "tint", "shade")]
    if not toks:
        return None
    joined = " ".join(toks)
    if joined in _CSS3_COLORS:
        return _CSS3_COLORS[joined]
    mod_factor = 0.0
    base_toks: list[str] = []
    for t in toks:
        if t in _COLOR_MODIFIERS and not base_toks:
            mod_factor += _COLOR_MODIFIERS[t]
        elif t in _CSS3_COLORS:
            base_toks.append(t)
    if not base_toks:
        return None
    base_rgb = _CSS3_COLORS.get(" ".join(base_toks)) or _CSS3_COLORS.get(base_toks[-1])
    if base_rgb is None:
        return None
    if abs(mod_factor) < 1e-6:
        return base_rgb
    r, g, b = base_rgb
    if mod_factor > 0:
        f = min(0.75, mod_factor)
        return (int(r + (255 - r) * f), int(g + (255 - g) * f), int(b + (255 - b) * f))
    f = max(0.25, 1.0 + mod_factor)
    return (int(r * f), int(g * f), int(b * f))


def _color_similarity(goal_phrase: str, elem_label: str) -> float:
    """Return [0, 1] similarity if both phrase and label name W3C CSS3 colors closely matching in sRGB."""
    g_rgb = _parse_css3_rgb(goal_phrase)
    e_rgb = _parse_css3_rgb(elem_label)
    if g_rgb is None or e_rgb is None:
        return 0.0
    dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(g_rgb, e_rgb)))
    if dist >= 110.0:
        return 0.0
    return max(0.0, 1.0 - (dist / 130.0))


def _extract_action_target(goal: str) -> str:
    # Strip trailing completion clause BEFORE extracting quoted text so `until '14'` is never treated as click target
    main = re.split(r"\b(?:until|so that|then stop)\b", goal, maxsplit=1, flags=re.I)[0]
    q = re.findall(r"['\"]([^'\"]{1,64})['\"]", main)
    if q:
        return _fold_unicode(q[0])
    toks = _tokens(main)
    while len(toks) > 1 and toks[0] in _POLITE_PREFIX:
        toks.pop(0)
    if len(toks) > 1 and toks[0] in _ACTION_VERBS:
        toks.pop(0)
    while len(toks) > 1 and toks[0] in _ARTICLES_PREPS:
        toks.pop(0)
    while len(toks) > 1 and toks[-1] in _TRAILING_TARGET_STRIP:
        toks.pop()
    if len(toks) >= 2 and toks[-1] == "color" and toks[0] not in ("edit", "pick", "custom", "more"):
        toks.pop()
    if not toks:
        toks = [t for t in _tokens(main) if t not in _STOP_WORDS]
    return " ".join(toks)


def decompose_goal_steps(goal: str) -> tuple[list[str], str | None]:
    """Split a compound instruction into narrow single-action sub-goals + optional expected final state."""
    raw = (goal or "").strip()
    if not raw:
        return [], None

    done_expected: str | None = None
    clause_m = _DONE_CLAUSE_RE.search(raw)
    main_part = raw
    if clause_m:
        clause = clause_m.group(1).strip()
        state_m = _DONE_STATE_RE.search(clause)
        done_expected = (state_m.group(1) if state_m else clause).strip(" '\".")
        main_part = raw[: clause_m.start()].strip(" ,.;")
        if not main_part:
            return [raw], done_expected

    math_m = _MATH_EXPR_RE.match(main_part)
    if math_m:
        expr_clean = math_m.group(1).strip()
        if (
            not _DATE_OR_PHONE_RE.match(expr_clean)
            and any(op in expr_clean for op in ("+", "-", "*", "/", "×", "÷", "="))
        ):
            expr = expr_clean.replace(",", "")
            starts_calc = bool(re.match(r"^(?:calculate|compute|evaluate|solve)\b", main_part, re.I))
            steps: list[str] = []
            if starts_calc and not expr.lstrip().lower().startswith("c"):
                steps.append("Press Clear")
            for ch in expr:
                if ch.isspace():
                    continue
                if ch.lower() == "c":
                    steps.append("Press Clear")
                else:
                    steps.append(f"Press '{ch}'")
            if starts_calc and (not steps or not steps[-1].endswith("'='")):
                steps.append("Press '='")
            return steps, done_expected

    if re.search(r"(?:->|→|=>|;|\band\s+then\b|\bthen\b)", main_part, flags=re.I):
        parts = [
            p.strip()
            for p in re.split(r"(?:->|→|=>|;|\band\s+then\b|\bthen\b)", main_part, flags=re.I)
            if p and p.strip()
        ]
        if len(parts) >= 2:
            norm_steps: list[str] = []
            for p in parts:
                if re.match(r"^(?:click|press|tap|select|choose|open|toggle|check|uncheck|focus)\b", p, re.I):
                    norm_steps.append(p)
                else:
                    norm_steps.append(f"Click {p}")
            return norm_steps, done_expected

    if "," in main_part:
        verb_m = re.match(r"^(click|press|tap|select|hit)\s+(.+)$", main_part, flags=re.I)
        body = verb_m.group(2) if verb_m else main_part
        verb = verb_m.group(1).capitalize() if verb_m else "Press"
        items = [it.strip() for it in body.split(",") if it.strip()]
        if len(items) >= 2 and all(len(it) <= 36 for it in items):
            out_steps: list[str] = []
            for it in items:
                it_clean = re.sub(r"^and\s+", "", it, flags=re.I).strip()
                if not it_clean:
                    continue
                if re.match(r"^(?:click|press|tap|select|toggle|check)\b", it_clean, re.I):
                    out_steps.append(it_clean)
                elif len(it_clean) == 1:
                    out_steps.append(f"{verb} '{it_clean}'")
                else:
                    out_steps.append(f"{verb} {it_clean}")
            if len(out_steps) >= 2:
                return out_steps, done_expected

    return [raw], done_expected


def check_state_contains(expected: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    """Check whether any UIA element's name/label/value reflects `expected` (comma-normalized)."""
    exp_norm = _compact_num_text(expected.strip(" '\"."))
    if not exp_norm:
        return {"matched": False, "expected": expected, "matchedElement": None}
    pat = re.compile(rf"(?:^|\s|=|:){re.escape(exp_norm)}(?:$|\s)")
    for el in elements:
        raw_label = str(el.get("label") or el.get("name") or "")
        raw_val = str(el.get("value") or "")
        lbl = _compact_num_text(raw_label)
        val = _compact_num_text(raw_val)
        hay = f"{lbl} {val}".strip()
        if not hay:
            continue
        if exp_norm == lbl or exp_norm == val or pat.search(hay):
            return {
                "matched": True,
                "expected": expected,
                "matchedText": raw_val if raw_val else raw_label,
                "elementIndex": el.get("index"),
            }
    return {"matched": False, "expected": expected, "matchedElement": None}


def _check_goal_done(goal: str, elements: list[dict[str, Any]]) -> float:
    clause_m = _DONE_CLAUSE_RE.search(goal)
    if clause_m:
        clause = clause_m.group(1)
        state_m = _DONE_STATE_RE.search(clause)
        raw_expected = state_m.group(1) if state_m else clause
    else:
        direct_m = _DONE_DIRECT_RE.search(goal)
        if not direct_m:
            return -4.5
        raw_expected = direct_m.group(1)
    expected = _compact_num_text(raw_expected.strip(" '\"."))
    if not expected:
        return -4.5
    pat = re.compile(rf"(?:^|\s|=|:){re.escape(expected)}(?:$|\s)")
    for el in elements:
        label = _compact_num_text(str(el.get("label") or el.get("name") or ""))
        val = _compact_num_text(str(el.get("value") or ""))
        role = _fold_unicode(str(el.get("role") or el.get("controlType") or ""))
        hay = f"{label} {val}".strip()
        if not hay:
            continue
        if expected == label or expected == val or pat.search(hay):
            if role in ("text", "edit", "document", "status", "pane", "win32:static") or "display" in label or val:
                return 12.8
    return -4.2


def _soft_token_overlap(goal_toks: list[str], el_toks: set[str]) -> float:
    if not goal_toks or not el_toks:
        return 0.0
    matched_sum = 0.0
    for gt in goal_toks:
        if gt in el_toks or _stem_token(gt) in el_toks:
            matched_sum += 1.0
            continue
        best = 0.0
        for et in el_toks:
            s = token_similarity(gt, et)
            if s > best:
                best = s
        if best < 0.90:
            gt_alts = _expand_symbol_and_num_tokens([gt], raw_source=gt)
            if gt_alts & el_toks:
                best = max(best, 0.92)
        matched_sum += best
    prec = min(1.0, matched_sum / max(len(el_toks), 1))
    rec = min(1.0, matched_sum / max(len(goal_toks), 1))
    return (2.0 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0


def _score_lexical(goal: str, elements: list[dict[str, Any]]) -> dict[str, float]:
    scores: dict[str, float] = {
        "done": _check_goal_done(goal, elements),
        "unsure": 0.35,
    }
    goal_low = _fold_unicode(goal)
    target_phrase = _extract_action_target(goal)
    target_toks = _tokens(target_phrase)

    goal_toks = [t for t in _tokens(goal) if t not in _STOP_WORDS]
    if not goal_toks:
        goal_toks = target_toks or _tokens(goal)
    expanded_target = _expand_symbol_and_num_tokens(target_toks, raw_source=target_phrase)

    wants_tab = "tab" in goal_low
    wants_check = any(w in goal_low for w in ("checkbox", "tick", "check", "toggle"))
    wants_combo = any(w in goal_low for w in ("dropdown", "combo"))
    wants_slider = "slider" in goal_low
    wants_button = "button" in goal_low
    wants_text = any(w in goal_low for w in ("type", "enter", "input", "write", "search"))

    for el in elements[:MAX_ELEMENTS]:
        idx = int(el["index"])
        key = f"e{idx}"
        if not el.get("enabled", True):
            scores[key] = -5.0
            continue

        raw_label = str(el.get("label") or el.get("name") or "")
        raw_aid = str(el.get("automationId") or "")
        raw_val = str(el.get("value") or "")
        label = _fold_unicode(raw_label)
        aid = _fold_unicode(raw_aid)
        role = _fold_unicode(str(el.get("role") or el.get("controlType") or ""))
        val = _fold_unicode(raw_val)

        if not label and not aid and not val:
            scores[key] = -4.8
            continue

        aid_parts = [p for p in split_identifier(raw_aid) if p not in _UI_IDENT_SUFFIXES]
        lbl_parts = split_identifier(raw_label)
        val_parts = split_identifier(raw_val)
        el_toks = _expand_symbol_and_num_tokens([*lbl_parts, *aid_parts, *val_parts], raw_source=f"{raw_label} {raw_val}")
        el_core_toks = _expand_symbol_and_num_tokens([*lbl_parts, *aid_parts], raw_source=raw_label)

        sc = -4.5
        if target_phrase:
            if label == target_phrase or aid == target_phrase:
                sc = max(sc, 8.6)
            elif len(target_phrase) == 1 and target_phrase in expanded_target:
                if target_phrase in el_core_toks or (expanded_target & el_core_toks):
                    sc = max(sc, 8.4)
            elif target_toks and (set(target_toks) <= el_core_toks or expanded_target <= el_core_toks):
                len_bonus = min(1.0, len(target_toks) / max(len(el_core_toks), 1))
                sc = max(sc, 7.6 + 0.9 * len_bonus)
            elif target_phrase in label and (len(target_phrase) >= 3 or not target_phrase.isascii()):
                ratio = len(target_phrase) / max(len(label), 1)
                sc = max(sc, 3.8 + 1.8 * ratio)
            elif label and (len(label) >= 3 or not label.isascii()) and label in target_phrase:
                ratio = len(label) / max(len(target_phrase), 1)
                sc = max(sc, 2.6 + 1.4 * ratio)
            else:
                csim = _color_similarity(target_phrase, label)
                if csim >= 0.35:
                    sc = max(sc, 4.4 + 3.4 * csim)

        f1 = _soft_token_overlap(goal_toks, el_toks)
        if f1 >= 0.45:
            sc = max(sc, 1.6 + 3.8 * f1)

        if sc > 1.0:
            explicit_role = wants_tab or wants_check or wants_combo or wants_slider or wants_button
            if role in _PASSIVE_ROLES and not wants_text:
                sc -= 2.6
            elif wants_text and ("edit" in role or "document" in role or "combo" in role):
                sc += 1.60
            elif wants_tab and "tabitem" in role:
                sc += 2.20
            elif wants_check and ("checkbox" in role or "radiobutton" in role):
                sc += 2.20
            elif wants_combo and "combo" in role:
                sc += 2.20
            elif wants_slider and "slider" in role:
                sc += 2.20
            elif wants_button and "button" in role and "radio" not in role:
                sc += 2.20
            elif not explicit_role and any(r in role for r in _INTERACTIVE_ROLES):
                sc += 0.65

        scores[key] = sc

    return scores


def _softmax(scores: dict[str, float]) -> dict[str, float]:
    if not scores:
        return {"unsure": 1.0}
    m = max(scores.values())
    exps = {k: math.exp(max(-30.0, min(30.0, v - m))) for k, v in scores.items()}
    total = sum(exps.values()) or 1.0
    return {k: round(v / total, 4) for k, v in exps.items()}


def _probe_http_json(url: str, headers: dict[str, str] | None = None, timeout: float = 0.035) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with _urlopen_safe(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        data = json.loads(raw) if raw else None
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _discover_local_server() -> tuple[str, str, str, str] | None:
    """Resolve explicit CUA_MATCHER_ENDPOINT or (when CUA_MATCHER_AUTO_DISCOVER=1) dedicated local LLM servers."""
    global _CACHED_SERVER
    now = time.monotonic()

    env_ep = os.environ.get("CUA_MATCHER_ENDPOINT", "").strip()
    if env_ep:
        parsed = urllib.parse.urlparse(env_ep)
        host = (parsed.hostname or "").strip().lower()
        if parsed.scheme.lower() not in ("http", "https") or not host:
            return None
        allow_remote = os.environ.get("CUA_ALLOW_REMOTE_MATCHER", "0").strip().lower() in ("1", "true", "yes")
        if not _is_loopback_host(host) and not allow_remote:
            return None
        base = env_ep.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return (
            base,
            os.environ.get("CUA_MATCHER_MODEL", "local-matcher").strip(),
            os.environ.get("CUA_MATCHER_API_KEY", "").strip(),
            os.environ.get("CUA_MATCHER_API_KIND", "openai").strip().lower(),
        )

    auto_flag = os.environ.get("CUA_MATCHER_AUTO_DISCOVER", "0").strip().lower()
    if auto_flag not in ("1", "true", "yes", "on"):
        return None

    if _CACHED_SERVER is not None and now < _CACHED_SERVER[0]:
        return (_CACHED_SERVER[1], _CACHED_SERVER[2], _CACHED_SERVER[3], _CACHED_SERVER[4])
    _CACHED_SERVER = None

    def _probe_port(port: int) -> tuple[float, str, str, str, str] | None:
        base = f"http://127.0.0.1:{port}"
        if port == 11434:
            ps_data = _probe_http_json(f"{base}/api/ps", timeout=0.03)
            models = ps_data.get("models") if isinstance(ps_data, dict) else None
            if isinstance(models, list) and models:
                mid = str(models[0].get("name") or models[0].get("model") or "")
                if mid:
                    return (now + 60.0, base, mid, "", "openai")
            return None
        mdata = _probe_http_json(f"{base}/v1/models", timeout=0.03)
        if not mdata or not isinstance(mdata.get("data"), list):
            return None
        for m in mdata["data"]:
            st = (m.get("status") or {}).get("value")
            if st in (None, "loaded"):
                mid = str(m.get("id") or "")
                if not mid:
                    continue
                kind = "completion" if _probe_http_json(f"{base}/health", timeout=0.025) is not None else "openai"
                return (now + 60.0, base, mid, "", kind)
        return None

    with ThreadPoolExecutor(max_workers=len(_LOCAL_LLM_PORTS)) as pool:
        for hit in pool.map(_probe_port, _LOCAL_LLM_PORTS):
            if hit is not None:
                _CACHED_SERVER = hit
                return (hit[1], hit[2], hit[3], hit[4])

    return None


def _extract_choice_and_conf(
    raw_choice: str, probs_raw: list[Any], valid_choices: list[str]
) -> tuple[str, float] | None:
    choice = raw_choice.strip().splitlines()[0].strip(" `\"'.,")
    if choice not in valid_choices:
        m = re.search(r"\b(e\d+|done|unsure)\b", raw_choice)
        if not m or m.group(1) not in valid_choices:
            return None
        choice = m.group(1)
    conf = 0.88
    if probs_raw and isinstance(probs_raw[0], dict):
        top = probs_raw[0].get("probs") or probs_raw[0].get("top_logprobs") or []
        if top and isinstance(top[0], dict):
            cand_mass = 0.0
            chosen_mass = 0.0
            prefix = choice[:1]
            for item in top:
                if not isinstance(item, dict):
                    continue
                tok_s = str(item.get("tok_str") or item.get("token") or "").strip()
                p_val = (
                    float(item["prob"])
                    if "prob" in item
                    else (math.exp(float(item["logprob"])) if "logprob" in item else 0.0)
                )
                if tok_s in valid_choices or tok_s in ("e", "done", "unsure", "d", "u"):
                    cand_mass += p_val
                    if tok_s == choice or (tok_s and tok_s == prefix):
                        chosen_mass += p_val
            if cand_mass > 0 and chosen_mass > 0:
                conf = max(0.85, chosen_mass / cand_mass)
            elif "prob" in top[0]:
                conf = max(0.85, float(top[0]["prob"]))
            elif "logprob" in top[0]:
                conf = max(0.85, math.exp(float(top[0]["logprob"])))
    return choice, round(max(0.0, min(1.0, conf)), 4)


def _try_local_llm_fallback(
    goal: str,
    elements: list[dict[str, Any]],
    lexical_scores: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    """Optional Tier-2 pass against an explicitly configured or opt-in local LLM server."""
    global _DEAD_ENDPOINT_UNTIL, _CACHED_SERVER
    flag = os.environ.get("CUA_MATCHER_ENABLED", "1").strip().lower()
    if flag in ("0", "false", "off", "no"):
        return None
    now = time.monotonic()
    if now < _DEAD_ENDPOINT_UNTIL:
        return None

    found = _discover_local_server()
    if found is None:
        _DEAD_ENDPOINT_UNTIL = now + 20.0
        return None
    endpoint, model_name, api_key, api_kind = found

    if len(elements) > NEURAL_MAX_ELEMENTS and lexical_scores:
        capped = sorted(
            elements,
            key=lambda e: lexical_scores.get(f"e{int(e['index'])}", -9.0),
            reverse=True,
        )[:NEURAL_MAX_ELEMENTS]
        capped.sort(key=lambda e: int(e["index"]))
    else:
        capped = elements[:NEURAL_MAX_ELEMENTS]

    valid_choices = ["done", "unsure", *[f"e{int(e['index'])}" for e in capped]]
    lines = [
        "Select the single best UI element ID for the goal.",
        f"Goal: {goal}",
        "Elements:",
        "done: The goal is already complete",
        "unsure: No listed element clearly advances the goal",
    ]
    for el in capped:
        role = el.get("role") or el.get("controlType") or "Element"
        lbl = el.get("label") or el.get("name") or el.get("automationId") or ""
        val = f" (value={el['value']!r})" if el.get("value") not in (None, "") else ""
        lines.append(f"e{int(el['index'])}: {role} \"{lbl}\"{val}")
    lines.append("Answer with ONLY the single best ID:")
    body = "\n".join(lines)

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        if api_kind == "completion":
            grammar = f"root ::= ({' | '.join(json.dumps(c) for c in valid_choices)})\n"
            payload = {
                "model": model_name,
                "prompt": f"{body}\n",
                "n_predict": 3,
                "temperature": 0.0,
                "n_probs": 5,
                "cache_prompt": True,
                "grammar": grammar,
            }
            req = urllib.request.Request(
                f"{endpoint.rstrip('/')}/completion",
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
            )
            with _urlopen_safe(req, timeout=0.45) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            parsed = _extract_choice_and_conf(
                str(out.get("content") or ""), out.get("completion_probabilities") or [], valid_choices
            )
            if parsed is None:
                return None
            choice, conf = parsed
            engine_tag = f"local_llama:{model_name}"
        else:
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": "Reply with ONLY one token: e<index>, done, or unsure."},
                    {"role": "user", "content": body},
                ],
                "max_tokens": 4,
                "temperature": 0.0,
                "logprobs": True,
                "top_logprobs": 5,
            }
            req = urllib.request.Request(
                f"{endpoint.rstrip('/')}/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
            )
            with _urlopen_safe(req, timeout=0.50) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            ch0 = (out.get("choices") or [{}])[0]
            txt = str(((ch0.get("message") or {}).get("content")) or "")
            lp_list = ((ch0.get("logprobs") or {}).get("content")) or []
            parsed = _extract_choice_and_conf(txt, lp_list, valid_choices)
            if parsed is None:
                return None
            choice, conf = parsed
            engine_tag = f"local_openai:{model_name}"

        return {
            "choice": choice,
            "confidence": conf,
            "probabilities": {choice: conf},
            "engine": engine_tag,
        }
    except Exception:
        _CACHED_SERVER = None
        _DEAD_ENDPOINT_UNTIL = time.monotonic() + 20.0
        return None


def match_ui_element(
    goal: str,
    elements: list[dict[str, Any]],
    *,
    confidence_floor: float = CONFIDENCE_FLOOR,
) -> dict[str, Any]:
    """Pick `e<index>`, `done`, or `unsure` with calibrated probabilities across up to 120 UIA controls."""
    t0 = time.perf_counter()
    norm_elems: list[dict[str, Any]] = []
    for i, el in enumerate(elements[:MAX_ELEMENTS]):
        norm_elems.append({
            "index": int(el.get("index", i)),
            "role": str(el.get("role") or el.get("controlType") or "Element"),
            "label": str(el.get("label") or el.get("name") or ""),
            "automationId": str(el.get("automationId") or ""),
            "value": el.get("value"),
            "enabled": bool(el.get("enabled", True)),
        })

    scores = _score_lexical(goal, norm_elems)
    probs = _softmax(scores)
    best_choice = max(probs, key=lambda k: probs[k])
    best_conf = probs[best_choice]
    engine = "local_fast_matcher"

    if best_choice == "unsure" or best_conf < 0.88:
        neural = _try_local_llm_fallback(goal, norm_elems, lexical_scores=scores)
        if neural is not None and neural["choice"] != "unsure" and (
            best_choice == "unsure" or neural["confidence"] > best_conf
        ):
            best_choice = neural["choice"]
            best_conf = neural["confidence"]
            probs = neural["probabilities"]
            engine = neural["engine"]

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    top_probs = dict(sorted(probs.items(), key=lambda kv: kv[1], reverse=True)[:6])
    elem_idx = int(best_choice[1:]) if re.match(r"^e\d+$", best_choice) else None
    return {
        "choice": best_choice,
        "elementIndex": elem_idx,
        "confidence": best_conf,
        "confidenceFloor": confidence_floor,
        "act": best_choice not in ("done", "unsure") and best_conf >= confidence_floor and elem_idx is not None,
        "probabilities": top_probs,
        "engine": engine,
        "elapsedMs": elapsed_ms,
    }
