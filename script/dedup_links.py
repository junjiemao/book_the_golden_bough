#!/usr/bin/env python3
"""
dedup_links.py — 修复章节中重复的 Obsidian [[wikilinks]]。

对每个章节文件，保留每个术语的第一个 [[wikilink]]，
将后续重复出现的 [[term]] / [[ref|display]] 还原为纯文本。

用法：
  python3 script/dedup_links.py                              # 全量处理
  python3 script/dedup_links.py --chapter "68 第六十八章 金枝.md"
  python3 script/dedup_links.py --dry-run                   # 预览，不写入
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def dedup_links(text: str) -> tuple[str, list[str]]:
    """
    扫描 text 中所有 [[ref]] / [[ref|display]] wikilinks，
    第一次出现保留，后续出现替换为裸文本（display 或 ref）。
    返回 (new_text, list_of_deduped_terms)。
    """
    seen: set[str] = set()
    deduped: list[str] = []
    result: list[str] = []
    pos = 0

    # 找到 YAML frontmatter 的结束位置，跳过不处理
    fm_end = 0
    fm = re.match(r"^---\n.*?\n---\n?", text, re.DOTALL)
    if fm:
        fm_end = fm.end()

    pattern = re.compile(r"\[\[([^\]\n|]+)(?:\|([^\]\n]+))?\]\]")

    for m in pattern.finditer(text):
        if m.start() < fm_end:
            # 在 frontmatter 内，原样保留
            result.append(text[pos:m.end()])
            pos = m.end()
            continue

        ref = m.group(1).strip()
        display = m.group(2).strip() if m.group(2) else ref

        result.append(text[pos:m.start()])
        pos = m.end()

        if ref not in seen:
            seen.add(ref)
            result.append(m.group(0))          # 保留第一个
        else:
            result.append(display)              # 还原为裸文本
            if ref not in deduped:
                deduped.append(ref)

    result.append(text[pos:])
    return "".join(result), deduped


def process_chapter(chapter_path: Path, dry_run: bool) -> None:
    text = chapter_path.read_text(encoding="utf-8")
    new_text, deduped = dedup_links(text)

    if new_text == text:
        return  # 无需修改，静默跳过

    if dry_run:
        print(f"  [DRY] {chapter_path.name}: 将修复 {deduped}")
    else:
        chapter_path.write_text(new_text, encoding="utf-8")
        print(f"  [OK]  {chapter_path.name}: 已修复 {deduped}")


def main() -> int:
    ap = argparse.ArgumentParser(description="修复章节中重复的 Obsidian [[wikilinks]]。")
    ap.add_argument("--chapters-dir", default="./golden_bough", help="章节 md 目录")
    ap.add_argument("--chapter", help="只处理指定章节文件名（含 .md）")
    ap.add_argument("--dry-run", action="store_true", help="预览，不写入文件")
    args = ap.parse_args()

    chapters_dir = Path(args.chapters_dir)
    files = sorted(f for f in chapters_dir.glob("*.md"))
    if args.chapter:
        files = [f for f in files if f.name == args.chapter]
        if not files:
            print(f"未找到章节: {args.chapter}")
            return 1

    for f in files:
        process_chapter(f, dry_run=args.dry_run)

    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
