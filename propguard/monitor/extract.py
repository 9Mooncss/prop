"""Deterministic content extraction and canonicalization (no LLM)."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from bs4 import BeautifulSoup

EXTRACTOR_VERSION = "extract/1.0.0"
_DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "template", "canvas", "button")
_BOILERPLATE_TAGS = ("nav", "footer", "header", "aside")
_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th", "dt", "dd", "blockquote", "pre",
               "summary", "caption", "figcaption")
_VOLATILE = [
    re.compile(r"©\s*\d{4}(\s*[-–]\s*\d{4})?"),
    re.compile(r"(?i)\b(\d+\s+(seconds?|minutes?|hours?)\s+ago)\b"),
    re.compile(r"(?i)\bcsrf[-_ ]?token\S*"),
]
_COOKIE = re.compile(r"(?i)(we use cookies|cookie (policy|settings|preferences)|accept all cookies)")


def extract_blocks(html: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:500]
    for t in soup(_DROP_TAGS):
        t.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    for t in main.find_all(_BOILERPLATE_TAGS):
        t.decompose()
    blocks: list[str] = []
    for el in main.find_all(_BLOCK_TAGS):
        if el.find(_BLOCK_TAGS):  # only leaf-level blocks, avoid duplicates of nested text
            continue
        txt = canonicalize(el.get_text(" ", strip=True))
        if txt and not _COOKIE.search(txt) and (not blocks or blocks[-1] != txt):
            blocks.append(txt)
    if not blocks:  # plain text / unusual markup fallback
        for line in main.get_text("\n").splitlines():
            txt = canonicalize(line)
            if txt and not _COOKIE.search(txt):
                blocks.append(txt)
    return title, blocks


def canonicalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = t.replace(" ", " ").replace("’", "'").replace("–", "-").replace("—", "-")
    for p in _VOLATILE:
        t = p.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def content_hash(blocks: list[str]) -> str:
    return hashlib.sha256("\n".join(blocks).encode()).hexdigest()


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9%.]+", text.lower()))


def fragment_match_score(fragment: str, blocks: list[str], window: int = 3) -> float:
    """Share of the fragment's tokens found within the best window of consecutive blocks."""
    ft = tokens(canonicalize(fragment))
    if not ft:
        return 0.0
    best = 0.0
    for i in range(len(blocks)):
        bt = tokens(" ".join(blocks[i:i + window]))
        best = max(best, len(ft & bt) / len(ft))
        if best == 1.0:
            break
    return best
