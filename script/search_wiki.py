#!/usr/bin/env python3
"""
search_wiki.py
功能：读取 terms/*.txt 中的术语，通过 Wikipedia 中文 API 获取词条摘要，
      在原术语文件旁边生成 <章节名>_wiki.txt。

用法：
  python3 script/search_wiki.py
  python3 script/search_wiki.py --chapter "01 第一章 林中之王.txt"
  python3 script/search_wiki.py --lang en   # 改用英文维基
  python3 script/search_wiki.py --merge     # 把 wiki 摘要直接追加到术语文件里
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Wikipedia REST API endpoint
WIKI_SUMMARY = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
# OpenSearch API (for fuzzy search fallback)
WIKI_SEARCH = "https://{lang}.wikipedia.org/w/api.php"

HEADERS = {"User-Agent": "GoldenBoughAnnotator/1.0 (educational tool)"}


# ─── Wikipedia helpers ────────────────────────────────────────────────────

def wiki_summary(term: str, lang: str = "zh") -> tuple[str, str]:
    """
    Returns (title, extract) from Wikipedia.
    Falls back to OpenSearch if exact title not found.
    Returns ("", "") on failure.
    """
    encoded = urllib.parse.quote(term, safe="")
    url = WIKI_SUMMARY.format(lang=lang, title=encoded)
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("type") == "disambiguation":
                return "", ""
            extract = data.get("extract", "").strip()
            title = data.get("title", term)
            if extract:
                # Trim to ~200 chars
                return title, extract[:220] + ("…" if len(extract) > 220 else "")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"      HTTP {e.code} for '{term}'")
    except Exception:
        pass

    # Fallback: OpenSearch
    search_params = urllib.parse.urlencode({
        "action": "opensearch",
        "search": term,
        "limit": 1,
        "namespace": 0,
        "format": "json",
    })
    search_url = WIKI_SEARCH.format(lang=lang) + "?" + search_params
    try:
        req = urllib.request.Request(search_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data and data[1]:
                found_title = data[1][0]
                if found_title.lower() != term.lower():
                    # Recursively try with the found title (prevent infinite loop)
                    return wiki_summary(found_title, lang)
    except Exception:
        pass

    return "", ""


# ─── Terms file parser ────────────────────────────────────────────────────

def parse_terms(path: Path) -> list[str]:
    """Return list of term names from a terms .txt file."""
    terms: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r'^【(.+)】\s*$', line.strip())
        if m:
            terms.append(m.group(1))
    return terms


# ─── Output helpers ───────────────────────────────────────────────────────

def process_terms_file(
    in_path: Path,
    out_dir: Path,
    lang: str,
    merge: bool,
) -> None:
    terms = parse_terms(in_path)
    if not terms:
        print(f"    [跳过] 未解析到术语: {in_path.name}")
        return

    results: list[tuple[str, str, str]] = []  # (orig_term, wiki_title, extract)
    for term in terms:
        wiki_title, extract = wiki_summary(term, lang=lang)
        if extract:
            results.append((term, wiki_title, extract))
            short = extract[:60].replace("\n", " ")
            print(f"      ✓ {term} → {wiki_title}: {short}…")
        else:
            print(f"      - {term}: 未找到词条")
        time.sleep(0.25)  # polite rate-limiting

    if not results:
        return

    if merge:
        # Append wiki block to the original file
        original = in_path.read_text(encoding="utf-8")
        block = ["\n" + "=" * 60 + "\n"]
        block.append("Wikipedia 摘要\n" + "=" * 60 + "\n\n")
        for orig, title, extract in results:
            block.append(f"【{orig}】（Wikipedia: {title}）\n")
            block.append(f"  {extract}\n\n")
        in_path.write_text(original + "".join(block), encoding="utf-8")
        print(f"    → 已追加到: {in_path.name}")
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_name = in_path.stem + "_wiki.txt"
        out_path = out_dir / out_name
        lines = [
            f"Wikipedia 摘要 — {in_path.stem}\n",
            "=" * 60 + "\n\n",
        ]
        for orig, title, extract in results:
            lines.append(f"【{orig}】（Wikipedia: {title}）\n")
            lines.append(f"  {extract}\n\n")
        out_path.write_text("".join(lines), encoding="utf-8")
        print(f"    → 写入: {out_path.name}")


# ─── Main ─────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="从 Wikipedia 搜索《金枝》术语摘要。")
    ap.add_argument("--terms-dir", default="./terms")
    ap.add_argument("--lang", default="zh", help="维基语言版本，默认 zh（中文），可填 en")
    ap.add_argument("--chapter", help="只处理指定术语文件名（含 .txt）")
    ap.add_argument("--merge", action="store_true", help="将 wiki 摘要追加写入原术语文件")
    args = ap.parse_args()

    terms_dir = Path(args.terms_dir)
    if not terms_dir.exists():
        print(f"术语目录不存在: {terms_dir}", file=sys.stderr)
        print("请先运行 annotate_chapters.py 生成术语文件。")
        return 1

    files = sorted(f for f in terms_dir.glob("*.txt") if "_wiki" not in f.name)
    if args.chapter:
        files = [f for f in files if f.name == args.chapter]
        if not files:
            print(f"未找到: {args.chapter}")
            return 1

    if not files:
        print("terms/ 目录中没有术语文件。请先运行 annotate_chapters.py。")
        return 1

    print(f"共 {len(files)} 个术语文件，语言: {args.lang}\n")
    for f in files:
        print(f"  {f.name}")
        process_terms_file(f, terms_dir, lang=args.lang, merge=args.merge)

    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
