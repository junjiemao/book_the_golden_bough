#!/usr/bin/env python3
"""
annotate_chapters.py
功能：
  1. 使用本地 LLM 从正文（不包括脚注）抽取术语 → terms/<章节名>.txt
  2. 在 YAML frontmatter 后插入 Obsidian callout 摘要

用法：
  python3 script/annotate_chapters.py
  python3 script/annotate_chapters.py --no-llm       # 仅使用结构化摘要，不调用 LLM
  python3 script/annotate_chapters.py --chapter "01 第一章 林中之王.md"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

_print_lock = Lock()

def _log(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)

# ─── 本地 LLM 客户端 ──────────────────────────────────────────────────────────
LLM_BASE_URL = "http://localhost:4141/v1"
LLM_MODEL = "gpt-5-mini"

_llm_client = None
HAVE_LLM = False
try:
    from openai import OpenAI  # type: ignore
    _llm_client = OpenAI(base_url=LLM_BASE_URL, api_key="sk-xxxxx")
    HAVE_LLM = True
except ImportError:
    print("[警告] openai 包未安装：pip install openai", file=sys.stderr)

CALLOUT_TYPE = "abstract"
CALLOUT_TITLE = "本章速览"

# ─── Frontmatter helpers ────────────────────────────────────────────────────

def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        return "", text
    second = text.index("---", 3)
    end = second + 3
    return text[:end], text[end:].lstrip("\n")


def fm_value(fm: str, key: str) -> str:
    m = re.search(rf'^{key}:\s*(.+)$', fm, re.MULTILINE)
    return m.group(1).strip() if m else ""

# ─── Terms extraction via LLM ───────────────────────────────────────────────

def _strip_footnotes(body: str) -> str:
    """移除脚注定义区和行内脚注标记，返回干净正文。"""
    # 删除所有 [^n]: ... 定义行
    body = re.sub(r'^\[\^\d+\]:.*$', '', body, flags=re.MULTILINE)
    # 删除行内 [^n] 标记
    body = re.sub(r'\[\^\d+\]', '', body)
    return body.strip()


def extract_terms_llm(body: str, chapter_title: str) -> list[dict]:
    """调用本地 LLM，从正文中抽取术语。返回 [{"name": ..., "definition": ...}]。"""
    if not HAVE_LLM or _llm_client is None:
        return []

    clean_body = _strip_footnotes(body)
    # 只取前 3000 字左右，避免 token 超限
    snippet = clean_body[:3000]

    prompt = (
        "你是一位为初中生讲解《金枝》（弗雷泽著）的老师。\n"
        "请阅读下面这段章节正文，从中找出对初中生来说陌生的专有名词，"
        "包括：神话人物、历史人物、地名、宗教/文化概念、仪式名称等。\n"
        "要求：\n"
        "1. 提取 5-20 个最重要的术语，不要收录普通动词或虚词\n"
        "2. 只输出 JSON 字符串数组，格式：[\"术语1\", \"术语2\", ...]\n"
        "3. 不要有任何额外文字，只有 JSON\n\n"
        f"章节：{chapter_title}\n"
        f"正文节选：\n{snippet}"
    )

    try:
        resp = _llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
            temperature=0.3,
        )
        raw = resp.choices[0].message.content.strip()
        # 提取 JSON 数组部分（防止模型输出了多余前后缀）
        json_match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not json_match:
            print(f"    [LLM 术语] 无法解析 JSON，原始输出：{raw[:200]}")
            return []
        items = json.loads(json_match.group(0))
        result = []
        for item in items:
            if isinstance(item, str):
                name = item.strip()
                if name:
                    result.append({"name": name, "definition": ""})
            elif isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                if name:
                    result.append({"name": name, "definition": item.get("definition", "")})
        return result
    except Exception as exc:
        print(f"    [LLM 术语失败] {exc}")
        return []


def write_terms_file(terms: list[dict], chapter_title: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [t['name'] for t in terms]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

# ─── Summary generation ────────────────────────────────────────────────────

def _sections(body: str) -> list[str]:
    return re.findall(r'^###\s+(.+)$', body, re.MULTILINE)


def _first_body_para(body: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if (s
                and not s.startswith('#')
                and not s.startswith('>')
                and not s.startswith('[^')
                and not s.startswith('---')
                and len(s) > 30):
            s = re.sub(r'\[\^\d+\]', '', s)
            return s[:130] + ('……' if len(s) > 130 else '')
    return ""


def build_fallback_summary(fm: str, body: str) -> str:
    title = fm_value(fm, "title")
    volume = fm_value(fm, "volume")
    sections = _sections(body)
    first_para = _first_body_para(body)

    parts: list[str] = []
    if volume:
        parts.append(f"【{volume}】")
    if sections:
        sec_str = "、".join(f"「{s}」" for s in sections[:5])
        more = "等" if len(sections) > 5 else ""
        parts.append(f"本章分为 {len(sections)} 节：{sec_str}{more}。")
    if first_para:
        parts.append(first_para)

    return " ".join(parts) or f"《金枝》{title}。"


def build_gpt_summary(fm: str, body: str) -> str:
    title = fm_value(fm, "title")
    clean_body = _strip_footnotes(body)
    body_snippet = clean_body[:2500]

    prompt = (
        "你是给初中生讲《金枝》（弗雷泽著）的老师。"
        "请用 120 字以内、通俗易懂的语言为以下章节写一段摘要。\n"
        "要求：①说明本章核心主题 ②举出最重要的 1-2 个例子或论点 "
        "③避免专业术语，遇到术语请简短解释 ④直接输出摘要，不加任何前缀。\n\n"
        f"章节标题：{title}\n"
        f"内容节选：\n{body_snippet}"
    )
    try:
        resp = _llm_client.chat.completions.create(  # type: ignore
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=250,
            temperature=0.4,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        print(f"    [LLM 摘要失败] {exc} — 使用结构化摘要")
        return build_fallback_summary(fm, body)

# ─── Callout helpers ───────────────────────────────────────────────────────

def make_callout(summary: str) -> str:
    lines = [f"> [!{CALLOUT_TYPE}] {CALLOUT_TITLE}"]
    for line in summary.splitlines():
        lines.append(f"> {line}".rstrip())
    return "\n".join(lines) + "\n"


def already_has_callout(body: str) -> bool:
    return bool(re.search(rf'^> \[!{CALLOUT_TYPE}\]', body, re.MULTILINE))

# ─── Process one file ─────────────────────────────────────────────────────

def process_chapter(
    md_path: Path,
    terms_dir: Path,
    use_llm: bool,
    force_callout: bool = False,
) -> None:
    text = md_path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)
    chapter_title = fm_value(fm, "title") or md_path.stem

    # 1. Terms extraction via LLM (正文，不含脚注)
    if use_llm:
        terms = extract_terms_llm(body, chapter_title)
        if terms:
            terms_path = terms_dir / f"{md_path.stem}.txt"
            write_terms_file(terms, chapter_title, terms_path)
            _log(f"  [{md_path.name}] 术语: {len(terms):2d} 条 → {terms_path.name}")
        else:
            _log(f"  [{md_path.name}] 术语: LLM 未返回结果，跳过")
    else:
        _log(f"  [{md_path.name}] 术语: 已跳过（未启用 LLM）")

    # 2. Callout
    if already_has_callout(body):
        if not force_callout:
            _log(f"  [{md_path.name}] callout: 已存在，跳过")
            return
        # 移除旧 callout 块再重写
        body = re.sub(
            rf'^> \[!{CALLOUT_TYPE}\] {CALLOUT_TITLE}\n(?:>.*\n)*',
            '',
            body,
            flags=re.MULTILINE,
        ).lstrip("\n")
        _log(f"  [{md_path.name}] callout: 覆盖旧内容")
    else:
        _log(f"  [{md_path.name}] callout: 新增")

    summary = build_gpt_summary(fm, body) if use_llm else build_fallback_summary(fm, body)
    callout = make_callout(summary)

    new_text = fm + "\n\n" + callout + "\n" + body
    md_path.write_text(new_text, encoding="utf-8")

# ─── Main ─────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="为《金枝》章节添加摘要 callout 并用 LLM 提取术语。")
    ap.add_argument("--chapters-dir", default="./golden_bough")
    ap.add_argument("--terms-dir", default="./terms")
    ap.add_argument("--no-llm", action="store_true", help="禁用 LLM，只生成结构化摘要，不提取术语")
    ap.add_argument("--force-callout", action="store_true", help="覆盖已存在的 callout（默认跳过）")
    ap.add_argument("--workers", type=int, default=4, help="并行线程数（默认 4）")
    ap.add_argument("--chapter", help="只处理指定文件名（测试用）")
    args = ap.parse_args()

    chapters_dir = Path(args.chapters_dir)
    terms_dir = Path(args.terms_dir)

    files = sorted(chapters_dir.glob("*.md"))
    if args.chapter:
        files = [f for f in files if f.name == args.chapter]
        if not files:
            print(f"未找到: {args.chapter}")
            return 1

    use_llm = not args.no_llm
    if use_llm and not HAVE_LLM:
        print("[警告] openai 包未安装，已禁用 LLM。\n")
        use_llm = False

    if use_llm:
        print(f"LLM: {LLM_BASE_URL}  model={LLM_MODEL}")
    print(f"共 {len(files)} 个章节，开始处理…\n")

    force_callout = args.force_callout
    workers = args.workers if not args.chapter else 1  # 单章测试不需要并发

    _log(f"并行数: {workers}")

    def _run(path: Path):
        _log(f"  [{path.name}] 开始")
        try:
            process_chapter(path, terms_dir, use_llm, force_callout=force_callout)
        except Exception as exc:
            _log(f"  [{path.name}] [错误] {exc}", file=sys.stderr)

    if workers <= 1:
        for path in files:
            _run(path)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run, p): p for p in files}
            for fut in as_completed(futures):
                exc = fut.exception()
                if exc:
                    _log(f"  [未捕获错误] {futures[fut].name}: {exc}", file=sys.stderr)

    _log(f"\n完成。callout 已写入章节文件，术语表在: {terms_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
