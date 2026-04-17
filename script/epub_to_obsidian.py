#!/usr/bin/env python3
"""Split an EPUB (unpacked) into Obsidian markdown files by chapter.

Default behavior:
- Reads metadata from content.opf
- Reads TOC from toc.ncx
- Exports depth-2 navPoints (chapters under a volume) to Markdown
- Writes YAML frontmatter with book metadata
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET


NS_OPF = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}

NS_NCX = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}

HTML_NS = "http://www.w3.org/1999/xhtml"


def read_opf_metadata(opf_path: Path) -> dict:
    tree = ET.parse(opf_path)
    root = tree.getroot()
    metadata = root.find("opf:metadata", NS_OPF)
    if metadata is None:
        return {}

    def text_of(tag: str) -> str:
        elem = metadata.find(tag, NS_OPF)
        return (elem.text or "").strip() if elem is not None else ""

    creators = metadata.findall("dc:creator", NS_OPF)
    author = ", ".join([(c.text or "").strip() for c in creators if (c.text or "").strip()])

    identifiers = metadata.findall("dc:identifier", NS_OPF)
    source_id = ""
    for ident in identifiers:
        if ident.get(f"{{{NS_OPF['opf']}}}scheme") in {"MOBI-ASIN", "uuid", "calibre"}:
            source_id = (ident.text or "").strip()
            if source_id:
                break

    return {
        "title": text_of("dc:title"),
        "author": author,
        "publisher": text_of("dc:publisher"),
        "language": text_of("dc:language"),
        "date": text_of("dc:date"),
        "source_id": source_id,
    }


def parse_toc_ncx(ncx_path: Path) -> list[dict]:
    tree = ET.parse(ncx_path)
    root = tree.getroot()
    nav_map = root.find("ncx:navMap", NS_NCX)
    if nav_map is None:
        return []

    entries: list[dict] = []

    def walk(nav_point: ET.Element, depth: int, parent_title: str | None) -> None:
        label = nav_point.find("ncx:navLabel/ncx:text", NS_NCX)
        content = nav_point.find("ncx:content", NS_NCX)
        title = (label.text or "").strip() if label is not None else ""
        src = content.get("src", "").strip() if content is not None else ""
        play_order = nav_point.get("playOrder", "")

        entries.append(
            {
                "title": title,
                "src": src,
                "depth": depth,
                "parent": parent_title,
                "play_order": play_order,
            }
        )

        for child in nav_point.findall("ncx:navPoint", NS_NCX):
            walk(child, depth + 1, title)

    for top in nav_map.findall("ncx:navPoint", NS_NCX):
        walk(top, 1, None)

    return entries


def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def normalize_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def parse_note_number(text: str) -> str | None:
    m = re.search(r"\[(\d+)\]", text or "")
    if m:
        return m.group(1)
    return None


def extract_footnote_ref_from_href(href: str) -> str | None:
    # Common EPUB pattern: partXXXX.html#a1 in body, and partXXXX.html#b1 in notes.
    if not href:
        return None
    m = re.search(r"#a(\d+)$", href)
    if m:
        return m.group(1)
    m = re.search(r"#b(\d+)$", href)
    if m:
        return m.group(1)
    return None


def render_inline(elem: ET.Element, footnotes: dict[str, str] | None = None) -> str:
    tag = strip_ns(elem.tag)
    if tag in {"script", "style"}:
        return ""

    parts: list[str] = []
    if elem.text:
        parts.append(normalize_text(elem.text))

    for child in list(elem):
        ctag = strip_ns(child.tag)
        if ctag in {"strong", "b"}:
            inner = render_inline(child)
            if inner:
                parts.append(f"**{inner}**")
        elif ctag in {"em", "i"}:
            inner = render_inline(child)
            if inner:
                parts.append(f"*{inner}*")
        elif ctag == "br":
            parts.append("  \n")
        elif ctag == "a":
            href = child.get("href", "")
            label = render_inline(child) or href
            note_num = parse_note_number(label) or extract_footnote_ref_from_href(href)
            if footnotes is not None and note_num and note_num in footnotes:
                parts.append(f"[^{note_num}]")
            elif href:
                parts.append(f"[{label}]({href})")
            else:
                parts.append(label)
        elif ctag == "img":
            alt = child.get("alt", "")
            src = child.get("src", "")
            if src:
                parts.append(f"![{alt}]({src})")
        elif ctag in {"ul", "ol"}:
            # handled in block rendering
            pass
        else:
            parts.append(render_inline(child, footnotes=footnotes))

        if child.tail:
            parts.append(normalize_text(child.tail))

    return "".join([p for p in parts if p])


def render_list(elem: ET.Element, ordered: bool, indent: int = 0) -> list[str]:
    lines: list[str] = []
    index = 1
    for li in elem.findall("./*"):
        if strip_ns(li.tag) != "li":
            continue

        prefix = f"{index}. " if ordered else "- "
        indent_str = " " * indent
        text = render_inline_excluding_lists(li)
        if text:
            lines.append(f"{indent_str}{prefix}{text}")
        else:
            lines.append(f"{indent_str}{prefix}")

        for child in list(li):
            ctag = strip_ns(child.tag)
            if ctag == "ul":
                lines.extend(render_list(child, ordered=False, indent=indent + 2))
            elif ctag == "ol":
                lines.extend(render_list(child, ordered=True, indent=indent + 2))

        if ordered:
            index += 1

    lines.append("")
    return lines


def render_inline_excluding_lists(elem: ET.Element, footnotes: dict[str, str] | None = None) -> str:
    parts: list[str] = []
    if elem.text:
        parts.append(normalize_text(elem.text))

    for child in list(elem):
        ctag = strip_ns(child.tag)
        if ctag in {"ul", "ol"}:
            continue
        parts.append(render_inline(child, footnotes=footnotes))
        if child.tail:
            parts.append(normalize_text(child.tail))

    return "".join([p for p in parts if p])


def render_block(
    elem: ET.Element,
    footnotes: dict[str, str] | None = None,
    skip_footnote_blocks: bool = False,
) -> list[str]:
    tag = strip_ns(elem.tag)

    if tag in {"script", "style"}:
        return []

    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(tag[1])
        text = render_inline(elem, footnotes=footnotes)
        return [f"{'#' * level} {text}".strip(), ""]

    if tag == "p":
        klass = elem.get("class", "")
        text = render_inline(elem, footnotes=footnotes)
        if skip_footnote_blocks and klass in {"note", "subtitle"} and text in {"注释", "注解"}:
            return []
        if skip_footnote_blocks and klass == "note":
            return []
        if text:
            return [text, ""]
        return []

    if tag == "blockquote":
        text = render_inline(elem, footnotes=footnotes)
        if not text:
            return []
        return [f"> {text}", ""]

    if tag == "ul":
        return render_list(elem, ordered=False)

    if tag == "ol":
        return render_list(elem, ordered=True)

    if tag == "hr":
        return ["---", ""]

    lines: list[str] = []
    for child in list(elem):
        lines.extend(render_block(child, footnotes=footnotes, skip_footnote_blocks=skip_footnote_blocks))
    return lines


def extract_footnotes(body: ET.Element) -> dict[str, str]:
    footnotes: dict[str, str] = {}
    for p in body.findall(f"./{{{HTML_NS}}}p"):
        if p.get("class", "") != "note":
            continue

        anchor = p.find(f"./{{{HTML_NS}}}a")
        note_num = None
        if anchor is not None:
            note_num = parse_note_number("".join(anchor.itertext()))
            if not note_num:
                note_num = extract_footnote_ref_from_href(anchor.get("href", ""))
            if not note_num and anchor.get("id", "").startswith("a"):
                note_num = anchor.get("id", "")[1:]

        rendered = render_inline(p)
        if not note_num:
            maybe_num = parse_note_number(rendered)
            if maybe_num:
                note_num = maybe_num

        if not note_num:
            continue

        # In note paragraphs, the first anchor is the marker like [1].
        # We drop it and keep the remaining textual note content.
        note_parts: list[str] = []
        if p.text:
            note_parts.append(normalize_text(p.text))
        for child in list(p):
            ctag = strip_ns(child.tag)
            if ctag != "a":
                note_parts.append(render_inline(child))
            if child.tail:
                note_parts.append(normalize_text(child.tail))
        note_text = "".join([x for x in note_parts if x]).strip()
        note_text = re.sub(r"^\s*\[\d+\]\s*", "", note_text).strip()
        if note_text:
            footnotes[note_num] = note_text

    return footnotes


def render_footnotes_section(footnotes: dict[str, str]) -> str:
    if not footnotes:
        return ""

    lines = []
    for n in sorted(footnotes.keys(), key=lambda x: int(x) if x.isdigit() else x):
        lines.append(f"[^{n}]: {footnotes[n]}")
    lines.append("")
    return "\n".join(lines)


def html_to_markdown(xhtml_path: Path) -> str:
    tree = ET.parse(xhtml_path)
    root = tree.getroot()
    body = root.find(f".//{{{HTML_NS}}}body")
    if body is None:
        return ""

    footnotes = extract_footnotes(body)

    lines: list[str] = []
    for child in list(body):
        lines.extend(render_block(child, footnotes=footnotes, skip_footnote_blocks=True))

    # collapse repeated blank lines
    cleaned: list[str] = []
    for line in lines:
        if line.strip() == "":
            if cleaned and cleaned[-1].strip() == "":
                continue
        cleaned.append(line.rstrip())

    content = "\n".join(cleaned).strip() + "\n"
    footnote_section = render_footnotes_section(footnotes)
    return content + ("\n" + footnote_section if footnote_section else "")


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name or "untitled"


def build_frontmatter(meta: dict, title: str, volume: str | None, source_file: str, play_order: str) -> str:
    lines = ["---"]
    lines.append(f"title: {title}")
    if volume:
        lines.append(f"volume: {volume}")
    if meta.get("title"):
        lines.append(f"book_title: {meta['title']}")
    if meta.get("author"):
        lines.append(f"author: {meta['author']}")
    if meta.get("publisher"):
        lines.append(f"publisher: {meta['publisher']}")
    if meta.get("language"):
        lines.append(f"language: {meta['language']}")
    if meta.get("date"):
        lines.append(f"date: {meta['date']}")
    if meta.get("source_id"):
        lines.append(f"source_id: {meta['source_id']}")
    lines.append(f"source_file: {source_file}")
    if play_order:
        lines.append(f"play_order: {play_order}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Split unpacked EPUB into Obsidian markdown files by chapter.")
    parser.add_argument(
        "--epub-dir",
        default="../book/golden_bough.epub",
        help="Path to unpacked EPUB directory (default: ../book/golden_bough.epub)",
    )
    parser.add_argument(
        "--out-dir",
        default="../book/obsidian",
        help="Output directory for markdown files (default: ../book/obsidian)",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=2,
        help="TOC depth to export (default: 2)",
    )
    parser.add_argument(
        "--clean-out-dir",
        action="store_true",
        help="Remove existing .md files in output directory before export.",
    )
    args = parser.parse_args()

    epub_dir = Path(args.epub_dir).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    opf_path = epub_dir / "content.opf"
    toc_path = epub_dir / "toc.ncx"

    if not opf_path.exists() or not toc_path.exists():
        print("Missing content.opf or toc.ncx in EPUB directory.", file=sys.stderr)
        return 1

    meta = read_opf_metadata(opf_path)
    toc_entries = parse_toc_ncx(toc_path)

    chapters = [
        e
        for e in toc_entries
        if e["depth"] == args.depth and e["src"]
    ]

    if not chapters:
        print("No chapters found at the requested TOC depth.", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)

    if args.clean_out_dir:
        for md in out_dir.glob("*.md"):
            md.unlink()

    used_names: dict[str, int] = {}
    for idx, entry in enumerate(chapters, start=0):
        src = entry["src"].split("#", 1)[0]
        source_file = src
        xhtml_path = epub_dir / src
        if not xhtml_path.exists():
            print(f"Skip missing source: {src}", file=sys.stderr)
            continue

        title = entry["title"] or xhtml_path.stem
        volume = entry.get("parent")
        frontmatter = build_frontmatter(meta, title, volume, source_file, entry.get("play_order", ""))
        body_md = html_to_markdown(xhtml_path)

        filename = f"{idx:02d} {sanitize_filename(title)}"
        if filename in used_names:
            used_names[filename] += 1
            filename = f"{filename}-{used_names[filename]}"
        else:
            used_names[filename] = 1

        out_path = out_dir / f"{filename}.md"
        out_path.write_text(frontmatter + body_md, encoding="utf-8")

    print(f"Done. Wrote {len(used_names)} files to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
