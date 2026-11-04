"""Chat Research Gate: bounded multi-round retrieval and citation audit for ChatGPT."""
import html
import ipaddress
import json
import re
import socket
from urllib.parse import urljoin, urlsplit

from ddgs import DDGS
import httpx
import trafilatura
from lxml import html as lxml_html
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Chat Research Gate", host="0.0.0.0", stateless_http=True, json_response=True)


def allowed(url):
    p = urlsplit(url)
    if p.scheme not in ("https", "http") or not p.hostname or p.username or p.password:
        return False
    h = p.hostname.lower()
    if h == "localhost" or h.endswith(".local"):
        return False
    try:
        return ipaddress.ip_address(h).is_global
    except ValueError:
        return True


def retrieve(url):
    if not allowed(url):
        return ""
    with httpx.Client(timeout=5.5, follow_redirects=False,
                      headers={"User-Agent": "ResearchGate/0.1"}) as client:
        for _ in range(3):
            host = urlsplit(url).hostname
            try:
                addresses = socket.getaddrinfo(host, 443)
                if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                    return ""
            except (OSError, ValueError):
                return ""
            try:
                with client.stream("GET", url) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        url = urljoin(url, response.headers.get("location", ""))
                        if not allowed(url):
                            return ""
                        continue
                    if response.status_code != 200:
                        return ""
                    content_type = response.headers.get("content-type", "")
                    if not any(t in content_type for t in ("text/html", "text/plain", "application/xhtml+xml")):
                        return ""
                    chunks, length = [], 0
                    for chunk in response.iter_bytes():
                        length += len(chunk)
                        if length > 900000:
                            return ""
                        chunks.append(chunk)
                raw = b"".join(chunks).decode("utf-8", "replace")
                if "text/plain" in content_type:
                    return raw[:40000]
                extracted = trafilatura.extract(raw, include_tables=True)
                if not extracted:
                    tree = lxml_html.fromstring(raw)
                    for element in tree.xpath("//script|//style|//nav|//footer|//noscript"):
                        element.drop_tree()
                    extracted = " ".join(tree.itertext())
                return html.unescape(extracted)[:40000]
            except Exception:
                return ""
    return ""


def subtopics(question, topics):
    if not question or not question.strip() or len(question) > 1800:
        raise ValueError("質問は1～1800文字で指定してください")
    if topics:
        rows = topics
    else:
        rows = [re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", line).strip()
                for line in question.splitlines()
                if re.match(r"^\s*(?:[-*•]|\d+[.)、])\s*", line)]
        if len(rows) < 2:
            rows = [question]
    unique = []
    for row in rows:
        s = " ".join(str(row).split())[:320]
        if s and s not in unique:
            unique.append(s)
    if not (1 <= len(unique) <= 12):
        raise ValueError("論点は1～12件に分解してください")
    return unique


def topic_terms(topic):
    """Extract useful terms, preserving software names and dotted versions."""
    ascii_terms = [x.lower() for x in re.findall(r"[a-zA-Z][\w.+-]{2,}|\d+(?:\.\d+)+", topic)]
    stop = {"the","what","which","where","when","how","why","and","for","are","can","does","with"}
    ascii_terms = [x for x in ascii_terms if x not in stop and len(x)>=3]
    # Domain-specific Japanese terms are more useful than whole Japanese sentences.
    jp_terms = [x for x in ("リリースノート", "公式", "バージョン", "仕様", "ソースコード",
                "ドキュメント", "実装", "不具合", "原因", "価格", "発売", "設定", "制限",
                "選手", "大会", "試合", "結果", "選考", "評価", "比較") if x in topic]
    jp_terms = [x for x in jp_terms if x not in ("公式","原因","結果","比較","評価","設定","制限")]
    if not ascii_terms and not jp_terms:
        chunks = re.findall(r"[一-龯々ァ-ヴー]{2,}",topic)
        jp_terms = [x for x in chunks if x not in ("方法","質問","場合","確認","情報")][:4]
    return list(dict.fromkeys(ascii_terms)), list(dict.fromkeys(jp_terms))


def search_query(topic, iteration):
    english, jp = topic_terms(topic)
    translate = {"リリースノート":"release notes", "仕様":"specification",
                 "ドキュメント":"documentation", "ソースコード":"source code",
                 "実装":"implementation", "不具合":"bug issue",
                 "価格":"pricing", "発売":"release", "大会":"tournament"}
    if iteration == 0:
        return topic[:165]
    if iteration == 1:
        base = " ".join(english + [translate.get(x,x) for x in jp])
        return (base + (" official" if "公式" in topic else ""))[:170] or topic[:165]
    return (" ".join(english + [translate.get(x,x) for x in jp]) + 
            " site:docs.python.org" if "python" in english else 
            topic[:120] + " 原資料 一次資料 詳細")[:170]


def relevant(topic, title, url, page):
    """Require core topic terms in the source, not only a successful HTTP response."""
    english, jp = topic_terms(topic)
    head = (title + " " + url).lower()
    body = page.lower()
    core = [x for x in english if x not in ("official","documentation")]
    if core:
        # Proper names and versions should appear in title or URL.
        if not any(x in head for x in core):
            return False, 0
        if any(x not in head and x not in body for x in core):
            return False, 0
        if any(re.match(r"^\d+(?:\.\d+)+$",x) for x in core) and not any(
                x in head for x in core if re.match(r"^\d+(?:\.\d+)+$",x)):
            return False, 0
    elif jp:
        if not any(x in head for x in jp):
            return False, 0
    else:
        return False, 0
    hits = sum(4 for x in core if x in head) + sum(1 for x in core if x in body)
    hits += sum(2 for x in jp if x in head) + sum(1 for x in jp if x in body)
    if core and hits < 5:
        return False, hits
    if jp and not core and hits < 3:
        return False, hits
    return True, hits


def excerpt(topic, text):
    english, jp = topic_terms(topic)
    words = sorted(english + jp, key=len, reverse=True)
    lower = text.lower()
    positions = [lower.find(w.lower()) for w in words if w.lower() in lower]
    if not positions:
        return ""
    position = min(positions)
    return text[max(0, position-80): min(len(text), position+380)].strip()


@mcp.tool()
def research_to_evidence(question: str, topics: list[str] | None = None,
                         rounds: int = 3, minimum_sources: int = 2) -> str:
    """Research directly in Chat with 1-3 search rounds and FULL PAGE retrieval.
    For research requests use this tool BEFORE writing the answer. It searches again
    when a topic has inadequate sourced excerpts. It returns an evidence table.
    Retrieval coverage is NOT a numerical probability that a conclusion is true.
    """
    topics = subtopics(question, topics)
    rounds = max(1, min(3, rounds))
    minimum_sources = max(1, min(3, minimum_sources))
    records = {topic: [] for topic in topics}
    seen = {topic: set() for topic in topics}
    issues = []
    all_count = 0
    for turn in range(rounds):
        for topic in topics:
            if len(records[topic]) >= minimum_sources:
                continue
            query = search_query(topic, turn)
            try:
                with DDGS(timeout=7) as engine:
                    results = list(engine.text(query, region="wt-wt" if turn else "jp-jp", backend="yahoo", max_results=12))
            except Exception as exc:
                issues.append("検索失敗: " + type(exc).__name__)
                continue
            # Fetch candidates that mention a core term in their title/URL first.
            english, jp = topic_terms(topic)
            candidates = sorted(results, key=lambda x: sum(
                1 for token in english + jp if token.lower() in
                ((x.get("title") or "") + " " + (x.get("href") or x.get("url") or "")).lower()
            ), reverse=True)
            for item in candidates:
                if len(records[topic]) >= minimum_sources or len(seen[topic]) >= 28:
                    break
                url = item.get("href") or item.get("url") or ""
                title = (item.get("title") or url)[:180]
                if not allowed(url) or url in seen[topic]:
                    continue
                seen[topic].add(url)
                core = [x for x in english if x not in ("official", "documentation")]
                if core and not any(x in (title + " " + url).lower() for x in core):
                    continue
                page = retrieve(url)
                if len(page) < 80:
                    continue
                is_relevant, score = relevant(topic, title, url, page)
                if not is_relevant:
                    continue
                quote = excerpt(topic, page)
                if not quote or quote not in page:
                    continue
                all_count += 1
                records[topic].append({"id": "E" + str(all_count),
                                       "title": title, "url": url,
                                       "quote": quote, "quote_verified": True,
                                       "relevance_checked": True, "relevance_score": score})
        completed = sum(len(records[t]) >= minimum_sources for t in topics)
        if 100 * completed / len(topics) >= 95:
            break
    results = [{"topic": t,
                "status": "fetched" if len(records[t]) >= minimum_sources else "needs_more_evidence",
                "source_count": len(records[t]), "sources": records[t]} for t in topics]
    coverage = round(100*sum(row["status"]=="fetched" for row in results)/len(results), 1)
    lines = ["| 論点 | 本文取得 | 結果 | URL |", "|---|---:|---|---|"]
    for row in results:
        links = ", ".join("[" + s["id"] + "](" + s["url"] + ")" for s in row["sources"])
        lines.append("| " + row["topic"].replace("|","｜") + " | " + str(row["source_count"]) +
                     " | " + row["status"] + " | " + (links or "なし") + " |")
    return json.dumps({"status": "ready" if coverage >= 95 else "incomplete",
                       "question": question, "retrieval_coverage_percent": coverage,
                       "relevance_filter": "v0.2.2; official names and versions must match",
                       "coverage_definition": "各論点について別URLの本文引用を必要件数だけ取得した割合。結論の正答確率ではない。",
                       "rounds_executed": turn+1,
                       "topics": results,
                       "evidence_table_markdown": "\n".join(lines),
                       "diagnostics": issues[:8]}, ensure_ascii=False)


@mcp.tool()
def verify_conclusions(research_json: str, conclusions_json: str) -> str:
    """Reject conclusions that lack topic-matched source IDs. Structural audit only.
    conclusions_json is [{topic, conclusion, evidence_ids:[E1,E2]}].
    """
    try:
        research, conclusions = json.loads(research_json), json.loads(conclusions_json)
    except (TypeError, ValueError):
        return json.dumps({"accepted": False, "errors": ["JSON不正"]}, ensure_ascii=False)
    if not isinstance(conclusions, list) or not isinstance(research, dict):
        return json.dumps({"accepted": False, "errors": ["引数形式不正"]}, ensure_ascii=False)
    topics = research.get("topics") or []
    if not topics:
        return json.dumps({"accepted": False, "errors": ["調査資料なし"]}, ensure_ascii=False)
    errors, passed = [], 0
    for item in topics:
        topic = item["topic"]
        matches = [c for c in conclusions if isinstance(c, dict) and c.get("topic")==topic]
        if len(matches)!=1 or len(str(matches[0].get("conclusion",""))) < 12:
            errors.append(topic + ": 具体的な結論が未提出")
            continue
        references = {s["id"] for s in item["sources"] if s["quote_verified"]}
        ids = matches[0].get("evidence_ids", [])
        if not ids or not isinstance(ids, list) or not all(x in references for x in ids):
            errors.append(topic + ": 出典IDが対応していない")
            continue
        passed += 1
    coverage = round(100 * passed / len(topics),1)
    accepted = coverage >= 95 and research.get("retrieval_coverage_percent",0) >= 95
    return json.dumps({"accepted": accepted, "citation_coverage_percent": coverage,
                       "errors": errors,
                       "limitation": "出典IDの存在のみ検証。結論の意味的な正誤は自動判定していない。"},
                       ensure_ascii=False)


@mcp.tool()
def gate_status() -> str:
    """Inspect availability of the no-paid-API research gate."""
    return json.dumps({"status": "available", "version": "0.2.2",
                       "tools": ["research_to_evidence", "verify_conclusions"],
                       "search": "ddgs", "paid_api_required": False},
                       ensure_ascii=False)


app = mcp.streamable_http_app()

# Mount iPhone-responsive web interface alongside the existing MCP endpoint.
from research_gate_mobile import mount_mobile
mount_mobile(app)
