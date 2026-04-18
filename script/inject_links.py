#!/usr/bin/env python3
"""
inject_links.py — 为章节 markdown 文件注入 Obsidian [[术语]] 内链。

对每个章节，读取对应的 terms/*.txt 术语列表，
检查 golden_bough/references/ 下是否存在对应的参考文件，
若存在则在章节正文中找到该术语的第一次出现并替换为 [[术语]]。

用法：
  python3 script/inject_links.py                              # 全量处理
  python3 script/inject_links.py --chapter "68 第六十八章 金枝.md"
  python3 script/inject_links.py --dry-run                   # 预览，不写入
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def sanitize(name: str) -> str:
    """与 search_wiki.py 保持一致的文件名安全化。"""
    return re.sub(r'[\\/:*?"<>|]', "-", name).strip()


def parse_terms(path: Path) -> list[str]:
    """读取术语文件，返回术语列表（一行一个）。"""
    terms: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^【(.+)】\s*$', line)
        terms.append(m.group(1) if m else line)
    return terms


def build_skip_ranges(text: str) -> list[tuple[int, int]]:
    """返回不应修改的字符区间列表（已排序）。"""
    skip: list[tuple[int, int]] = []

    # YAML frontmatter
    fm = re.match(r"^---\n.*?\n---\n?", text, re.DOTALL)
    if fm:
        skip.append((0, fm.end()))

    # Callout / blockquote 行 (> ...)
    for m in re.finditer(r"^>.*$", text, re.MULTILINE):
        skip.append((m.start(), m.end()))

    # 围栏代码块 ```...```
    for m in re.finditer(r"```[\s\S]*?```", text):
        skip.append((m.start(), m.end()))

    # 行内代码 `...`
    for m in re.finditer(r"`[^`\n]+`", text):
        skip.append((m.start(), m.end()))

    # 已有的 [[wikilinks]]
    for m in re.finditer(r"\[\[[^\]\n]+\]\]", text):
        skip.append((m.start(), m.end()))

    # Markdown 链接 / 图片 [text](url)
    for m in re.finditer(r"!?\[[^\]\n]*\]\([^\n)]*\)", text):
        skip.append((m.start(), m.end()))

    # 脚注引用标记 [^n] 和脚注定义 [^n]: ...
    for m in re.finditer(r"\[\^[^\]\n]+\](?::.*)?", text, re.MULTILINE):
        skip.append((m.start(), m.end()))

    # 标题行 ## Foo
    for m in re.finditer(r"^#{1,6} .+$", text, re.MULTILINE):
        skip.append((m.start(), m.end()))

    return sorted(skip)


def in_skip(start: int, end: int, skip_ranges: list[tuple[int, int]]) -> bool:
    for s, e in skip_ranges:
        if s >= end:
            break
        if s < end and start < e:
            return True
    return False


def inject_link(text: str, term: str, ref_stem: str) -> tuple[str, bool]:
    """
    在 text 中找到 term 的第一次出现（跳过保护区），替换为 wikilink。
    若 ref_stem == term，写为 [[term]]；否则写为 [[ref_stem|term]]。
    """
    skip = build_skip_ranges(text)
    pattern = re.compile(re.escape(term))
    for m in pattern.finditer(text):
        if in_skip(m.start(), m.end(), skip):
            continue
        link = f"[[{ref_stem}]]" if ref_stem == term else f"[[{ref_stem}|{term}]]"
        return text[: m.start()] + link + text[m.end() :], True
    return text, False


def process_chapter(
    chapter_path: Path,
    references_dir: Path,
    terms_dir: Path,
    dry_run: bool,
) -> None:
    terms_file = terms_dir / (chapter_path.stem + ".txt")
    if not terms_file.exists():
        print(f"  [跳过] 无术语文件: {chapter_path.stem}.txt")
        return

    terms = parse_terms(terms_file)
    if not terms:
        return

    # 只链接已有 reference 文件的术语
    linkable: list[tuple[str, str]] = []
    for term in terms:
        ref_stem = sanitize(term)
        if (references_dir / f"{ref_stem}.md").exists():
            linkable.append((term, ref_stem))

    if not linkable:
        print(f"  [--] {chapter_path.name}: 无可链接术语（待运行 search_wiki.py）")
        return

    text = chapter_path.read_text(encoding="utf-8")
    original = text
    injected: list[str] = []

    for term, ref_stem in linkable:
        # 已有该术语的任何 wikilink → 跳过，避免多次运行重复链接
        if re.search(r'\[\[' + re.escape(ref_stem) + r'[\]|]', text):
            continue
        text, changed = inject_link(text, term, ref_stem)
        if changed:
            injected.append(term)

    if text == original:
        print(f"  [--] {chapter_path.name}: 无新增链接（已全部链接？）")
        return

    if dry_run:
        print(f"  [DRY] {chapter_path.name}: 将链接 {injected}")
    else:
        chapter_path.write_text(text, encoding="utf-8")
        print(f"  [OK]  {chapter_path.name}: 已链接 {injected}")


def main() -> int:
    ap = argparse.ArgumentParser(description="为章节 markdown 注入 Obsidian 术语内链。")
    ap.add_argument("--chapters-dir",   default="./golden_bough",            help="章节 md 目录")
    ap.add_argument("--references-dir", default="./golden_bough/references", help="参考文件目录")
    ap.add_argument("--terms-dir",      default="./terms",                   help="术语 txt 目录")
    ap.add_argument("--chapter", help="只处理指定章节文件名（含 .md）")
    ap.add_argument("--dry-run", action="store_true", help="预览，不写入文件")
    args = ap.parse_args()

    chapters_dir   = Path(args.chapters_dir)
    references_dir = Path(args.references_dir)
    terms_dir      = Path(args.terms_dir)

    files = sorted(f for f in chapters_dir.glob("*.md"))
    if args.chapter:
        files = [f for f in files if f.name == args.chapter]
        if not files:
            print(f"未找到章节: {args.chapter}")
            return 1

    if not files:
        print("没有找到章节文件。")
        return 1

    print(f"共 {len(files)} 个章节，references: {references_dir}\n")
    for f in files:
        process_chapter(f, references_dir, terms_dir, dry_run=args.dry_run)

    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
