#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["scrapling[fetchers]", "html2text"]
# ///
"""
Universal web content extractor (Scrapling + html2text).
Returns clean Markdown with headings, links, images, lists, and code blocks.

Usage:
  uv run fetch.py <url> [max_chars] [--stealth] [--save [dir]] [--json]

Modes:
  (default)   Fast HTTP fetch via Fetcher — works for most sites (~1-3s)
  --stealth   Headless browser via StealthyFetcher — for JS-rendered or
              anti-scraping sites like WeChat, Zhihu, Juejin (~5-15s)

Options:
  --save [dir]  Save Markdown to a file. Default directory: ./download/
                Filename is derived from the URL slug.
  --json        Output as JSON with metadata.

Examples:
  uv run fetch.py https://sspai.com/post/73145
  uv run fetch.py https://sspai.com/post/73145 --save
  uv run fetch.py https://sspai.com/post/73145 --save ./my-articles
  uv run fetch.py https://mp.weixin.qq.com/s/xxx 30000 --stealth --save
"""

import sys
import os
import re
import json
import logging
from pathlib import Path
from urllib.parse import urlparse, unquote

# Ensure UTF-8 output on Windows (avoid GBK encoding errors with Chinese content)
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def check_dependencies():
    """Check if required packages are installed and provide install instructions."""
    missing = []
    try:
        import scrapling  # noqa: F401
    except ImportError:
        missing.append("scrapling")
    try:
        import html2text  # noqa: F401
    except ImportError:
        missing.append("html2text")

    if missing:
        import shutil

        if shutil.which("uv"):
            hint = f"  uv run {__file__} <url>   (dependencies are resolved automatically)"
        else:
            hint = (
                f"  Option 1 (recommended — isolated):\n"
                f"    pip install uv\n"
                f"    uv run {__file__} <url>\n"
                f"\n"
                f"  Option 2 (global install):\n"
                f"    pip install {' '.join(missing)}"
            )
        print(
            f"Error: missing dependencies: {', '.join(missing)}\n"
            f"\n"
            f"{hint}",
            file=sys.stderr,
        )
        sys.exit(1)


def fix_lazy_images(html_raw):
    """
    Promote data-src to src for lazy-loaded images (WeChat, Zhihu, etc.).
    Many Chinese platforms use data-src for the real image URL while src
    holds a tiny placeholder. html2text only reads src, so we swap them.
    """
    def _replace(m):
        tag = m.group(0)
        data_src = m.group(1)
        # Remove existing src="..." to avoid duplicate src attributes
        tag = re.sub(r'\ssrc="[^"]*"', '', tag)
        # Remove the data-src="..." itself
        tag = re.sub(r'\sdata-src="[^"]*"', '', tag)
        # Insert the real src after <img
        tag = tag.replace('<img', f'<img src="{data_src}"', 1)
        return tag

    return re.sub(r'<img[^>]*?\sdata-src="([^"]+)"[^>]*?>', _replace, html_raw)


def fix_wechat_code_blocks(html_raw):
    """
    Fix WeChat's code snippet blocks:
    1. Remove line-number <ul> lists (rendered as '* * * *' by html2text).
    2. Insert newlines between consecutive <code> tags inside <pre>, so each
       line renders on its own line instead of being concatenated.
    """
    # Remove line-number lists
    html_raw = re.sub(
        r'<ul\s+class="code-snippet__line-index[^"]*"[^>]*>.*?</ul>',
        '',
        html_raw,
        flags=re.DOTALL,
    )
    # Insert newline between consecutive </code><code> inside <pre>
    html_raw = re.sub(r'</code>\s*<code>', '\n', html_raw)
    return html_raw


def fix_wechat_fake_headings(html_raw):
    """
    WeChat's rich editor abuses heading tags (h1-h3) for styling — the actual
    font-size is often 14-17px, i.e. body text. Demote these fake headings to
    plain <p> tags so html2text doesn't turn every paragraph into a heading.

    Heuristic: if the heading's inline style contains font-size <= 17px, or
    the heading contains a <span> with font-size <= 17px, it's fake.
    """
    def _demote(m):
        tag_html = m.group(0)
        open_tag = m.group(1)  # e.g. "h1" or "h3"
        # Check for font-size in the tag or its children
        sizes = re.findall(r'font-size:\s*(\d+)px', tag_html)
        if sizes:
            max_size = max(int(s) for s in sizes)
            if max_size <= 17:
                # Replace opening and closing heading tags with <p>
                tag_html = re.sub(rf'^<{open_tag}\b', '<p', tag_html, count=1)
                tag_html = re.sub(rf'</{open_tag}>\s*$', '</p>', tag_html, count=1)
        return tag_html

    return re.sub(
        r'<(h[1-3])\b[^>]*>.*?</\1>',
        _demote,
        html_raw,
        flags=re.DOTALL,
    )


def fix_wechat_inline_code_links(html_raw):
    """
    WeChat wraps standalone URLs in <pre><code>...</code></pre> blocks,
    producing fenced code blocks in Markdown. When a <pre> block's only
    text content is a short line with a URL (e.g. "开源地址：https://..."),
    convert it to a plain <p> so the URL renders as regular text.
    """
    def _replace(m):
        inner = m.group(1)
        text = re.sub(r'<[^>]+>', '', inner).strip()
        # Only convert if it's short (< 200 chars) and contains a URL
        if len(text) < 200 and re.search(r'https?://', text):
            return f'<p>{text}</p>'
        return m.group(0)

    return re.sub(
        r'<pre[^>]*>.*?<code[^>]*>(.*?)</code>.*?</pre>',
        _replace,
        html_raw,
        flags=re.DOTALL,
    )


# CSS selectors in priority order — the first match with enough content wins.
# Covers most blog/article platforms without needing per-site customization.
CONTENT_SELECTORS = [
    "article",
    "main",
    ".post-content",
    ".entry-content",
    ".article-content",
    ".article-body",
    ".article-detail",         # 36kr
    ".article-holder",         # InfoQ
    ".post_body",              # 163.com (NetEase)
    ".markdown-body",          # GitHub
    ".Post-RichText",          # Zhihu
    "#article_content",        # CSDN
    ".article-area",           # Juejin
    ".ssa-article",            # Toutiao
    '[role="article"]',
    '[itemprop="articleBody"]',
]

# WeChat has a unique DOM structure — try these first for mp.weixin.qq.com
WECHAT_SELECTORS = [
    "div#js_content",
    "div.rich_media_content",
]

# Minimum characters for a selector match to be considered "real content"
MIN_CONTENT_LENGTH = 200


def html_to_markdown(html_raw, max_chars=30000, is_wechat=False):
    """Convert raw HTML to clean Markdown."""
    import html2text

    html_raw = fix_lazy_images(html_raw)
    html_raw = fix_wechat_code_blocks(html_raw)
    if is_wechat:
        html_raw = fix_wechat_fake_headings(html_raw)
        html_raw = fix_wechat_inline_code_links(html_raw)

    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = False
    h.body_width = 0       # No line wrapping
    h.skip_internal_links = True
    h.ignore_emphasis = False
    h.mark_code = True     # Use fenced code blocks (```) instead of indentation

    md = h.handle(html_raw)
    # Convert [code]...[/code] to fenced code blocks
    md = md.replace("[code]", "```").replace("[/code]", "```")
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md[:max_chars]


def extract_title(page, url):
    """
    Extract page title from HTML. Tries multiple sources in order:
    og:title, <title> tag, first <h1>.
    """
    # Try og:title first (most reliable for articles)
    og = page.css('meta[property="og:title"]')
    if og:
        val = og[0].attrib.get("content", "").strip()
        if val:
            return val

    # Try <title> tag
    title_els = page.css("title")
    if title_els:
        val = title_els[0].text.strip()
        if val:
            # Strip common suffixes like " - 少数派" or " | Medium"
            val = re.split(r"\s*[|\-–—_]\s*(?=[^|\-–—_]*$)", val)[0].strip()
            return val

    # Try first <h1>
    h1 = page.css("h1")
    if h1:
        val = h1[0].text.strip()
        if val:
            return val

    return ""


def extract_content(page, url, max_chars=30000):
    """
    Try content selectors to find the article body.
    Returns (markdown_text, matched_selector, title).
    """
    title = extract_title(page, url)
    is_wechat = "mp.weixin.qq.com" in url
    selectors = (WECHAT_SELECTORS + CONTENT_SELECTORS) if is_wechat else CONTENT_SELECTORS

    for selector in selectors:
        els = page.css(selector)
        if els:
            md = html_to_markdown(els[0].html_content, max_chars, is_wechat=is_wechat)
            if len(md) >= MIN_CONTENT_LENGTH:
                return md, selector, title

    # Fallback: convert the entire page
    md = html_to_markdown(page.html_content, max_chars, is_wechat=is_wechat)
    return md, "body(fallback)", title


def _suppress_scrapling_logs():
    """Scrapling's logger is noisy (deprecation warnings, fetch info). Silence it."""
    logging.getLogger("scrapling").setLevel(logging.CRITICAL)


def fetch_fast(url, max_chars=30000, timeout=15):
    """
    Fast HTTP fetch — no JavaScript execution.
    Works for most blogs and static sites.
    """
    from scrapling.fetchers import Fetcher
    _suppress_scrapling_logs()

    page = Fetcher().get(url, timeout=timeout, stealthy_headers=True)
    return extract_content(page, url, max_chars)


def fetch_stealth(url, max_chars=30000, timeout=30000):
    """
    Headless browser fetch — executes JavaScript, bypasses anti-scraping.
    Required for: WeChat articles, Zhihu, Juejin, and other JS-rendered pages.
    Slower (~5-15s) but more reliable for protected content.
    """
    from scrapling.fetchers import StealthyFetcher
    _suppress_scrapling_logs()

    page = StealthyFetcher().fetch(
        url,
        headless=True,
        network_idle=True,
        timeout=timeout,
    )
    return extract_content(page, url, max_chars)


def fetch(url, max_chars=30000, stealth=False):
    """
    Main entry point. Fetches URL and returns (markdown, selector, mode, title).
    If stealth=False, tries fast mode first and falls back to stealth
    when the result is too short (likely a JS-rendered page).
    """
    if stealth:
        md, selector, title = fetch_stealth(url, max_chars)
        return md, selector, "stealth", title

    # Try fast mode first
    md, selector, title = fetch_fast(url, max_chars)

    # If fast mode got barely any content, the page likely needs JS rendering
    if len(md) < MIN_CONTENT_LENGTH:
        try:
            md_stealth, sel_stealth, title_stealth = fetch_stealth(url, max_chars)
            if len(md_stealth) > len(md):
                return md_stealth, sel_stealth, "stealth(auto-fallback)", title_stealth
        except Exception:
            pass  # Stick with fast mode result

    return md, selector, "fast", title


DEFAULT_SAVE_DIR = "download"


def url_to_filename(url):
    """
    Derive a short, filesystem-safe filename (without extension) from a URL.
    Examples:
      https://sspai.com/post/73145          -> sspai-post-73145
      https://mp.weixin.qq.com/s/AbC123     -> weixin-AbC123
      https://zhuanlan.zhihu.com/p/12345    -> zhihu-p-12345
      https://example.com/my-great-article  -> example-my-great-article
    """
    parsed = urlparse(url)
    domain = parsed.hostname or "unknown"

    # Simplify common domains
    domain_short = domain.replace("www.", "")
    for prefix in ("mp.", "zhuanlan.", "blog."):
        domain_short = domain_short.replace(prefix, "")
    # Take first part of domain (e.g., "sspai" from "sspai.com")
    domain_short = domain_short.split(".")[0]

    # Build slug from path
    path = unquote(parsed.path).strip("/")
    if not path:
        path = "index"
    # Replace separators with hyphens, keep only safe chars
    slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff-]", "-", path)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")

    # Truncate to avoid overly long filenames
    name = f"{domain_short}-{slug}"[:80].rstrip("-")
    return name


def save_markdown(md, url, save_dir, title="", mode="", selector=""):
    """Save markdown content with YAML frontmatter to a .md file. Returns the file path."""
    from datetime import datetime, timezone

    dir_path = Path(save_dir)
    dir_path.mkdir(parents=True, exist_ok=True)

    filename = url_to_filename(url) + ".md"
    filepath = dir_path / filename

    # Avoid overwriting: append a number if file exists
    if filepath.exists():
        base = filepath.stem
        for i in range(1, 100):
            candidate = dir_path / f"{base}-{i}.md"
            if not candidate.exists():
                filepath = candidate
                break

    # Build YAML frontmatter
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Escape quotes in title for YAML safety
    safe_title = title.replace('"', '\\"') if title else ""
    frontmatter_lines = [
        "---",
        f'title: "{safe_title}"',
        f"source: {url}",
        f"fetched_at: {now}",
        f"fetch_mode: {mode}",
        f"selector: {selector}",
        "---",
    ]
    frontmatter = "\n".join(frontmatter_lines)

    # Prepend title as H1 if not already present in content
    body = md
    if title and not md.lstrip().startswith("# "):
        body = f"# {title}\n\n{md}"

    content = f"{frontmatter}\n\n{body}\n"
    filepath.write_text(content, encoding="utf-8")
    return str(filepath)


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: python3 fetch.py <url> [max_chars] [--stealth]\n"
            "\n"
            "Options:\n"
            "  max_chars   Maximum output characters (default: 30000)\n"
            "  --stealth   Use headless browser for JS-rendered pages\n"
            "  --json      Output as JSON with metadata\n",
            file=sys.stderr,
        )
        sys.exit(1)

    url = sys.argv[1]
    args = sys.argv[2:]

    stealth = "--stealth" in args
    json_output = "--json" in args

    # Parse --save [dir]
    save_dir = None
    if "--save" in args:
        idx = args.index("--save")
        # Check if next arg is a directory (not another flag, not a number)
        if idx + 1 < len(args) and not args[idx + 1].startswith("--") and not args[idx + 1].isdigit():
            save_dir = args[idx + 1]
            args = args[:idx] + args[idx + 2:]
        else:
            save_dir = DEFAULT_SAVE_DIR
            args = args[:idx] + args[idx + 1:]

    args = [a for a in args if not a.startswith("--")]
    max_chars = int(args[0]) if args else 30000

    try:
        md, selector, mode, title = fetch(url, max_chars, stealth=stealth)

        if save_dir:
            filepath = save_markdown(md, url, save_dir, title=title, mode=mode, selector=selector)
            print(f"Saved to: {filepath}", file=sys.stderr)

        if json_output:
            result = {
                "url": url,
                "title": title,
                "mode": mode,
                "selector": selector,
                "content_length": len(md),
                "content": md,
            }
            if save_dir:
                result["saved_to"] = filepath
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(md)

    except Exception as e:
        error_msg = f"Error fetching {url}: {type(e).__name__}: {e}"
        if json_output:
            print(json.dumps({"url": url, "error": error_msg}, ensure_ascii=False))
        else:
            print(error_msg, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    check_dependencies()
    main()
