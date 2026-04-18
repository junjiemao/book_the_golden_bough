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
import datetime
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from openai import OpenAI as _OpenAI
    _openai_available = True
except ImportError:
    _openai_available = False

LLM_BASE_URL = "http://localhost:4141/v1"
LLM_MODEL    = "gpt-5-mini"

# Wikipedia REST API endpoint
WIKI_SUMMARY = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
# OpenSearch API (for fuzzy search fallback)
WIKI_SEARCH = "https://{lang}.wikipedia.org/w/api.php"

HEADERS = {
    "User-Agent": "GoldenBoughAnnotator/1.0 (educational tool)",
    "Accept-Language": "zh-hans, zh;q=0.9",
}


# ─── LLM translation helper ──────────────────────────────────────────────

def translate_terms_llm(terms: list[str]) -> dict[str, str]:
    """
    通过本地 LLM 将中文术语批量翻译为英文 Wikipedia 搜索词。
    返回 {中文术语: 英文译名} 字典。
    """
    if not _openai_available:
        print("    [LLM] openai 未安装，跳过翻译")
        return {}
    if not terms:
        return {}

    client = _OpenAI(base_url=LLM_BASE_URL, api_key="sk-xxxxx")
    terms_json = json.dumps(terms, ensure_ascii=False)
    prompt = (
        "你是一个学术翻译助手。以下是《金枝》一书中的中文术语列表（JSON 数组）。"
        "请将每个术语翻译为最适合在英文维基百科中搜索的英文标题，"
        "返回 JSON 对象，key 为原中文术语，value 为英文 Wikipedia 标题。"
        "只返回 JSON，不要解释。\n\n" + terms_json
    )
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        # 提取 JSON 对象
        m = re.search(r'\{[\s\S]+\}', raw)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        print(f"    [LLM] 翻译失败: {e}")
    return {}


# ─── Wikipedia helpers ────────────────────────────────────────────────────

def wiki_summary(term: str, lang: str = "zh") -> tuple[str, str, str, str]:
    """
    Returns (title, description, extract, actual_lang) from Wikipedia.
    Falls back to OpenSearch if exact title not found.
    If zh fails, automatically retries with English Wikipedia.
    Returns ("", "", "", "") on complete failure.
    """
    # zh-hans is a variant; Wikipedia host is still zh.wikipedia.org
    host_lang = "zh" if lang.startswith("zh") else lang
    encoded = urllib.parse.quote(term, safe="")
    url = WIKI_SUMMARY.format(lang=host_lang, title=encoded)
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("type") == "disambiguation":
                return "", "", "", ""
            extract = data.get("extract", "").strip()
            description = data.get("description", "").strip()
            title = data.get("title", term)
            if extract:
                # Trim to ~200 chars
                return title, description, extract[:220] + ("…" if len(extract) > 220 else ""), lang
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"      HTTP {e.code} for '{term}'")
    except Exception:
        pass

    # Fallback: OpenSearch on same language
    search_params_dict = {
        "action": "opensearch",
        "search": term,
        "limit": 1,
        "namespace": 0,
        "format": "json",
    }
    if lang.startswith("zh"):
        search_params_dict["variant"] = "zh-hans"
    search_params = urllib.parse.urlencode(search_params_dict)
    search_url = WIKI_SEARCH.format(lang=host_lang) + "?" + search_params
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

    # Last resort: English Wikipedia fallback
    if lang != "en":
        title_en, desc_en, extract_en, _ = wiki_summary(term, lang="en")
        if extract_en:
            return title_en, desc_en, extract_en, "en"

    return "", "", "", ""


# ─── Terms file parser ────────────────────────────────────────────────────

def parse_terms(path: Path) -> list[str]:
    """Return list of term names from a terms .txt file (one term per line)."""
    terms: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        # Support both legacy 【term】 format and current plain-line format
        m = re.match(r'^【(.+)】\s*$', line)
        terms.append(m.group(1) if m else line)
    return terms


# ─── Output helpers ───────────────────────────────────────────────────────

def process_terms_file(
    in_path: Path,
    out_dir: Path,
    lang: str,
    merge: bool,
    llm_retry: bool = False,
) -> None:
    terms = parse_terms(in_path)
    if not terms:
        print(f"    [跳过] 未解析到术语: {in_path.name}")
        return

    results: list[tuple[str, str, str, str, str]] = []  # (orig_term, wiki_title, description, extract, actual_lang)
    failed: list[str] = []  # 中文无法找到，待 LLM 翻译后重试

    for term in terms:
        # 跳过已存在的 reference 文件
        safe_name = re.sub(r'[\\/:*?"<>|]', "-", term).strip()
        if (out_dir / f"{safe_name}.md").exists():
            continue
        wiki_title, description, extract, actual_lang = wiki_summary(term, lang=lang)
        if extract:
            results.append((term, wiki_title, description, extract, actual_lang))
            en_mark = " [EN]" if actual_lang == "en" else ""
            short = extract[:60].replace("\n", " ")
            print(f"      ✓{en_mark} {term} → {wiki_title}: {short}…")
        else:
            failed.append(term)
            print(f"      - {term}: 未找到词条")
        time.sleep(0.25)

    # LLM 翻译后重试
    if llm_retry and failed:
        print(f"    [LLM] 翻译 {len(failed)} 个失败术语…")
        translations = translate_terms_llm(failed)
        for term in failed:
            en_query = translations.get(term, "").strip()
            if not en_query:
                continue
            wiki_title, description, extract, actual_lang = wiki_summary(en_query, lang="en")
            if extract:
                results.append((term, wiki_title, description, extract, actual_lang))
                short = extract[:60].replace("\n", " ")
                print(f"      ✓ [LLM→EN] {term} →({en_query})→ {wiki_title}: {short}…")
                failed.remove(term)
            time.sleep(0.25)

    # 记录仍失败的术语到日志
    if failed:
        failed_log = out_dir / "_failed_terms.txt"
        existing = set(failed_log.read_text(encoding="utf-8").splitlines()) if failed_log.exists() else set()
        new_entries = [t for t in failed if t not in existing]
        if new_entries:
            out_dir.mkdir(parents=True, exist_ok=True)
            with failed_log.open("a", encoding="utf-8") as fh:
                for t in new_entries:
                    fh.write(t + "\n")

    if not results:
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    chapter_stem = in_path.stem

    for orig, title, description, extract, actual_lang in results:
        host_lang = "zh" if actual_lang.startswith("zh") else actual_lang
        wiki_url = f"https://{host_lang}.wikipedia.org/wiki/{urllib.parse.quote(title)}"
        safe_name = re.sub(r'[\\/:*?"<>|]', "-", orig).strip()
        out_path = out_dir / f"{safe_name}.md"
        is_en = actual_lang == "en"
        ref_type = "wiki-reference-en" if is_en else "wiki-reference-zh"
        ref_source = "wikipedia-en" if is_en else "wikipedia-zh"

        lines = [
            "---\n",
            f'title: "{orig}"\n',
            f"type: {ref_type}\n",
            f"source: {ref_source}\n",
            f"wiki_lang: {actual_lang}\n",
            f'wiki_url: "{wiki_url}"\n',
            f'chapter: "{chapter_stem}"\n',
            f'created: "{today}"\n',
            'tags: ["reference", "wiki", "zh", "golden-bough"]\n',
            "---\n",
            "\n",
            "> [!info] 维基百科\n" if not is_en else "> [!info] Wikipedia (EN)\n",
            f"> [{title}]({wiki_url})\n",
            "\n",
        ]
        if description:
            lines.append(f"**{description}**\n")
            lines.append("\n")
        lines.append(f"{extract}\n")
        lines.append("\n")
        lines.append("---\n")
        lines.append("\n")
        lines.append("## 参见\n")
        lines.append("\n")
        if is_en:
            lines.append(f"- [English Wikipedia ↗]({wiki_url})\n")
        else:
            lines.append(f"- [中文维基百科 ↗]({wiki_url})\n")
        lines.append("\n")

        out_path.write_text("".join(lines), encoding="utf-8")
        print(f"      → {out_path.name}")

    if merge:
        original = in_path.read_text(encoding="utf-8")
        block = ["\n" + "=" * 60 + "\n"]
        block.append("Wikipedia 摘要\n" + "=" * 60 + "\n\n")
        for orig, title, extract in results:
            block.append(f"【{orig}】（Wikipedia: {title}）\n")
            block.append(f"  {extract}\n\n")
        in_path.write_text(original + "".join(block), encoding="utf-8")
        print(f"    → 已追加到: {in_path.name}")


# ─── Main ─────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="从 Wikipedia 搜索《金枝》术语摘要。")
    ap.add_argument("--terms-dir", default="./terms")
    ap.add_argument("--out-dir", default="./golden_bough/references", help="输出目录（默认 ./golden_bough/references）")
    ap.add_argument("--lang", default="zh-hans", help="维基语言版本，默认 zh-hans（简体中文），可填 en")
    ap.add_argument("--chapter", help="只处理指定术语文件名（含 .txt）")
    ap.add_argument("--retry-missing", action="store_true", help="只处理尚无 reference 文件的术语（跳过已有的）")
    ap.add_argument("--llm-retry", action="store_true", help="对 Wikipedia 搜索失败的术语，调用 LLM 翻译后再次搜索英文维基")
    ap.add_argument("--merge", action="store_true", help="将 wiki 摘要追加写入原术语文件")
    args = ap.parse_args()

    terms_dir = Path(args.terms_dir)
    out_dir = Path(args.out_dir)
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
        process_terms_file(f, out_dir, lang=args.lang, merge=args.merge, llm_retry=args.llm_retry)
    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
