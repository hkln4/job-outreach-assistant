"""Hidden-content stripping for fetched pages, before any of their text reaches
a prompt. The <<<UNTRUSTED>>> fence (app/llm/untrusted.py) tells the model not
to obey page text; this removes the text a visitor would never see in the
first place - the usual hiding spot for instructions aimed at an AI reader:

- HTML level (`sanitize_html`): elements a browser wouldn't render (`hidden`,
  `aria-hidden`, inline styles that hide or push text off-screen), comments,
  `<template>` and `<svg>` text.
- Character level (`sanitize_text`): zero-width characters, bidi controls,
  soft hyphens and Unicode tag characters (invisible ASCII look-alikes).
- Instruction-shaped lines ("ignore previous instructions", "sen bir yapay
  zekasın") are flagged, not removed - they may be odd but visible copy, and
  the owner decides.

Everything removed or flagged lands in a `SanitizeReport`, stored with the
research. Hidden menus and cookie banners are routine, so only real targeting
signals make `suspicious` true (see the property).
"""
import html as html_lib
import re
import unicodedata
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, CData, Comment, Tag

MAX_EXCERPTS = 10
EXCERPT_CHARS = 200
# Some sites ship the whole page hidden and fade it in with JavaScript. A
# hidden element holding this share of the page's text is that wrapper, not a
# hidden block - kept, so research doesn't come back empty. Its text stays
# visible to the instruction check below.
WRAPPER_TEXT_SHARE = 0.6
NEVER_REMOVED = frozenset({"html", "body", "main"})

ZERO_WIDTH = frozenset("\u200b\u200c\u200d\u2060\ufeff")
BIDI_OVERRIDES = frozenset("\u202d\u202e")  # LRO / RLO - flip how text reads
BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u2066\u2067\u2068\u2069") | BIDI_OVERRIDES
SOFT_HYPHEN = "\u00ad"
TAG_CHARS_START, TAG_CHARS_END = 0xE0000, 0xE007F

# Off-screen if pushed at least this far negative, per unit.
_OFFSCREEN_MIN = {"px": 999, "pt": 750, "em": 50, "rem": 50, "%": 100, "vw": 100, "vh": 100}
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_CSS_ESCAPE_RE = re.compile(r"\\([0-9a-f]{1,6})\s?", re.I)
_ZERO = r"[-+]?0*\.?0+(?:px|em|rem|pt|%|vw|vh)?"
_HIDING_STYLE_RES = [
    re.compile(r"(?:^|;)display:none\b"),
    re.compile(r"(?:^|;)visibility:(?:hidden|collapse)\b"),
    re.compile(rf"(?:^|;)opacity:{_ZERO}(?:;|$|!)"),
    re.compile(rf"(?:^|;)font-size:{_ZERO}(?:;|$|!)"),
    re.compile(r"(?:^|;)color:transparent\b"),
]
_OFFSCREEN_RE = re.compile(r"(?:^|;)(?:left|top|right|bottom|text-indent|margin-left|margin-top):-(\d+(?:\.\d+)?)(px|pt|em|rem|%|vw|vh)")
_ZERO_BOX_RE = re.compile(rf"(?:^|;)(?:max-)?(?:width|height):{_ZERO}(?:;|$|!)")
_OVERFLOW_HIDDEN_RE = re.compile(r"(?:^|;)overflow(?:-[xy])?:(?:hidden|clip)\b")

# Matched against text folded by _fold() (lowercase, Turkish letters to ASCII),
# so one pattern covers "talimatları" and "talimatlari". Tuned against job-ad
# copy: "you are an AI engineer" or "As an Assistant, you will..." must not
# match, so AI-identity phrases need the clause to end right after them.
_END = r"\s*(?:[.,;:!?]|$|and\b|who\b|that\b|reading\b|ve\b)"
_AI = r"(?:ai|a\.i\.|llm|large language model|language model|chatbot|gpt|chatgpt|claude|gemini)"
INSTRUCTION_RES = [
    # English
    re.compile(r"\b(?:ignore|disregard|forget|override)\b.{0,30}\b(?:previous|prior|above|earlier|preceding|all|any|your)\b.{0,20}\b(?:instructions?|prompts?|directions|guidelines)\b"),
    re.compile(rf"\b(?:if )?you are (?:now )?(?:an?|the) (?:{_AI}|ai assistant|ai agent|virtual assistant)(?: (?:assistant|model|agent|system))?{_END}"),
    re.compile(rf"\bas (?:an?|the) (?:{_AI}|ai assistant|ai model|ai agent)\s*,"),
    re.compile(r"\b(?:system|developer) (?:prompt|message|instructions?)\s*:"),
    re.compile(r"\[(?:system|inst)\]|<\|?(?:system|im_start)\|?>"),
    re.compile(r"\bnew instructions?\s*:"),
    re.compile(rf"\b(?:note|message|instructions?) (?:to|for) (?:the |any )?(?:{_AI}|ai assistant|ai agent|ai model)s?\b"),
    re.compile(r"\bdo not (?:tell|mention|reveal) (?:this|these|that|it|anything) (?:to )?(?:the )?(?:user|owner|human|applicant|candidate)\b"),
    # Turkish (folded: ı->i, ş->s, ğ->g, ü->u, ö->o, ç->c)
    re.compile(r"\b(?:onceki|yukaridaki|daha onceki|tum|butun)\b.{0,25}\b(?:talimat|komut|yonerge)\w*\b.{0,15}\b(?:yok say|gormezden gel|unut|dikkate alma|umursama|gecersiz)"),
    re.compile(rf"\bsen (?:artik )?bir (?:(?:yapay zeka|ai|dil modeli|sohbet botu|chatbot)(?:\s*(?:asistani|modeli))?(?:sin|sun)?|asistansin){_END}"),
    re.compile(r"\b(?:yapay zeka|dil modeli|yapay zeka asistani|yapay zeka modeli) olarak\s*,"),
    re.compile(r"\b(?:yapay zekaya|ai'?[ay]a|llm'?e|dil modeline) (?:not|mesaj|talimat)\b"),
    re.compile(r"\beger (?:bir )?(?:yapay zeka|ai|dil modeli|llm)\w*\s*(?:isen|\w*san|\w*sen)\b"),
    re.compile(r"\bsistem (?:istemi|komutu|mesaji|talimati)\s*:"),
    re.compile(r"\b(?:kullaniciya|sahibine) (?:soyleme|bahsetme|gosterme)\b"),
]

_TURKISH_FOLD = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
                               "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c"})


@dataclass
class SanitizeReport:
    counts: dict[str, int] = field(default_factory=dict)
    hidden_excerpts: list[dict] = field(default_factory=list)
    instruction_lines: list[dict] = field(default_factory=list)
    tag_messages: list[str] = field(default_factory=list)

    def add(self, category: str, n: int = 1) -> None:
        if n:
            self.counts[category] = self.counts.get(category, 0) + n

    def merge(self, other: "SanitizeReport") -> None:
        for category, n in other.counts.items():
            self.add(category, n)
        self.hidden_excerpts.extend(other.hidden_excerpts)
        self.instruction_lines.extend(other.instruction_lines)
        self.tag_messages.extend(other.tag_messages)

    @property
    def suspicious(self) -> bool:
        """Signals of text aimed at a machine reader. Plain hidden blocks don't
        count - menus, modals and cookie banners are hidden all the time."""
        return bool(
            self.counts.get("tag_character")
            or self.counts.get("bidi_override")
            or self.counts.get("zero_width_in_word")
            or self.instruction_lines
        )

    def to_dict(self) -> dict:
        return {
            "suspicious": self.suspicious,
            "counts": dict(self.counts),
            "hidden_excerpts": self.hidden_excerpts[:MAX_EXCERPTS],
            "instruction_lines": self.instruction_lines[:MAX_EXCERPTS],
            "tag_messages": self.tag_messages[:MAX_EXCERPTS],
        }


def _fold(text: str) -> str:
    return text.translate(_TURKISH_FOLD).casefold()


def _normalize_style(style: str) -> str:
    """Inline style as the browser would read it: entities decoded (twice, for
    double-encoded values), CSS comments and escapes resolved, no whitespace."""
    value = html_lib.unescape(html_lib.unescape(style))
    value = _CSS_COMMENT_RE.sub("", value)
    value = _CSS_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), value)
    return re.sub(r"\s+", "", value).lower()


def style_hides(style: str) -> bool:
    s = _normalize_style(style)
    if not s:
        return False
    if any(r.search(s) for r in _HIDING_STYLE_RES):
        return True
    for amount, unit in _OFFSCREEN_RE.findall(s):
        if float(amount) >= _OFFSCREEN_MIN[unit]:
            return True
    return bool(_ZERO_BOX_RE.search(s) and _OVERFLOW_HIDDEN_RE.search(s))


def _hidden_reason(tag: Tag) -> str | None:
    if tag.has_attr("hidden"):
        return "hidden_attribute"
    if str(tag.get("aria-hidden", "")).strip().lower() == "true":
        return "aria_hidden"
    if tag.has_attr("style") and style_hides(str(tag["style"])):
        return "hidden_style"
    return None


def _excerpt(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:EXCERPT_CHARS] + ("…" if len(text) > EXCERPT_CHARS else "")


def find_instruction_lines(text: str, report: SanitizeReport, where: str, url: str | None = None) -> None:
    for line in text.splitlines():
        folded = _fold(line)
        if folded.strip() and any(r.search(folded) for r in INSTRUCTION_RES):
            entry = {"text": _excerpt(line), "where": where}
            if url:
                entry["url"] = url
            report.instruction_lines.append(entry)


def sanitize_html(html: str, report: SanitizeReport, url: str | None = None) -> str:
    """The page with everything a browser wouldn't show removed."""
    soup = BeautifulSoup(html, "html.parser")

    for node in soup.find_all(string=lambda s: isinstance(s, (Comment, CData))):
        report.add("comment")
        node.extract()

    for name in ("template", "svg"):
        for tag in soup.find_all(name):
            if not tag.decomposed:
                report.add(name)
                tag.decompose()

    page_chars = len(soup.get_text(strip=True)) or 1
    for tag in soup.find_all(True):
        if tag.decomposed:  # inside a hidden element removed earlier
            continue
        reason = _hidden_reason(tag)
        if reason is None:
            continue
        hidden_text = tag.get_text("\n", strip=True)
        if tag.name in NEVER_REMOVED or len(hidden_text) / page_chars >= WRAPPER_TEXT_SHARE:
            report.add("hidden_wrapper_kept")
            continue
        report.add(reason)
        if hidden_text:
            visible_hidden_text = sanitize_text(hidden_text, report, url=url, check_instructions=False)
            if len(report.hidden_excerpts) < MAX_EXCERPTS:
                entry = {"reason": reason, "text": _excerpt(visible_hidden_text)}
                if url:
                    entry["url"] = url
                report.hidden_excerpts.append(entry)
            # The most likely place for an injected instruction is text nobody sees.
            find_instruction_lines(visible_hidden_text, report, where="hidden", url=url)
        tag.decompose()

    return str(soup)


def sanitize_text(text: str, report: SanitizeReport, url: str | None = None, check_instructions: bool = True) -> str:
    """`text` without invisible characters; instruction-shaped lines flagged."""
    out: list[str] = []
    tag_message: list[str] = []
    for i, ch in enumerate(text):
        cp = ord(ch)
        if TAG_CHARS_START <= cp <= TAG_CHARS_END:
            report.add("tag_character")
            if 0x20 <= cp - TAG_CHARS_START < 0x7F:
                tag_message.append(chr(cp - TAG_CHARS_START))
            continue
        if tag_message:
            report.tag_messages.append(_excerpt("".join(tag_message)))
            tag_message = []
        if ch in ZERO_WIDTH:
            report.add("zero_width")
            prev = text[i - 1] if i else ""
            nxt = next((c for c in text[i + 1:] if c not in ZERO_WIDTH and c != SOFT_HYPHEN), "")
            if prev.isalnum() and nxt.isalnum() and not _joins_script(prev, nxt):
                report.add("zero_width_in_word")
            continue
        if ch in BIDI_CONTROLS:
            report.add("bidi_control")
            if ch in BIDI_OVERRIDES:
                report.add("bidi_override")
            continue
        if ch == SOFT_HYPHEN:
            report.add("soft_hyphen")
            continue
        out.append(ch)
    if tag_message:
        report.tag_messages.append(_excerpt("".join(tag_message)))

    cleaned = "".join(out)
    if check_instructions:
        find_instruction_lines(cleaned, report, where="visible", url=url)
    return cleaned


def _joins_script(prev: str, nxt: str) -> bool:
    """ZWJ/ZWNJ between letters is legitimate shaping in scripts like Arabic,
    Persian and the Indic ones - only Latin/Cyrillic/Greek words are suspicious."""
    def script(c: str) -> str:
        return unicodedata.name(c, "").split(" ")[0]

    return script(prev) not in ("LATIN", "CYRILLIC", "GREEK", "DIGIT") and script(prev) == script(nxt)
