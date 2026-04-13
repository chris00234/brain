#!/opt/homebrew/bin/python3
"""Format RAG search results into agent-ready context.

Usage:
  context_format.py <query> [options]

Options:
  -c, --collection  Collection(s) (default: all)
  -n, --limit       Max results (default: 5)

Output: formatted context block ready to paste into agent prompt.
"""
import json
import subprocess
import sys
import argparse
from datetime import datetime


def search(query, collections, limit):
    cmd = [sys.executable, '/Users/chrischo/server/brain/brain_core/search.py',
           query, '-c', collections, '-n', str(limit), '--json']
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return json.loads(result.stdout) if result.stdout.strip() else []


def format_context(query, results):
    """Format results into clean agent-ready context."""
    if not results:
        return f"[RAG] No results for: {query}"

    lines = []
    lines.append(f"[RAG Context] Query: \"{query}\" — {len(results)} results")
    lines.append(f"[Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M')}]")
    lines.append("")

    for i, r in enumerate(results):
        score = r['score']
        # Only include high-confidence results
        if score < 0.4:
            continue

        confidence = "HIGH" if score >= 0.6 else "MEDIUM" if score >= 0.5 else "LOW"
        source_short = r['source'].replace('/Users/chrischo/', '~/')
        agent_tag = f" ({r['agent']})" if r['agent'] else ""
        service_tag = f" [{r['service']}]" if r['service'] else ""

        lines.append(f"### [{confidence}] {source_short}{agent_tag}{service_tag}")

        # Trim content to useful length
        content = r['content'].strip()
        if len(content) > 500:
            content = content[:500] + "..."

        lines.append(content)
        lines.append("")

    if not any(line.startswith("### ") for line in lines):
        return f"[RAG] Low confidence results for: {query} — no reliable matches."

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description="RAG Context Formatter")
    parser.add_argument("query", help="Search query")
    parser.add_argument("-c", "--collection", default="all")
    parser.add_argument("-n", "--limit", type=int, default=5)
    args = parser.parse_args()

    results = search(args.query, args.collection, args.limit)
    formatted = format_context(args.query, results)
    print(formatted)


if __name__ == '__main__':
    main()
