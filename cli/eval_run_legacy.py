#!/opt/homebrew/bin/python3
"""Run RAG evaluation set and report accuracy. Tests both direct and unified search."""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, '/Users/chrischo/server/brain/brain_core')
from search import hybrid_search

import subprocess

EVAL_SET = Path('/Users/chrischo/server/brain/cli/eval_set.json')


def run_query_direct(query, collection):
    if collection == "all":
        collections = ["knowledge", "experience", "context", "semantic_memory", "obsidian"]
    else:
        collections = [collection]
    return hybrid_search(query, collections, limit=5, use_keyword=True)


def run_query_unified(query):
    cmd = ['/opt/homebrew/bin/python3', '/Users/chrischo/server/brain/brain_core/search_unified.py',
           query, '-n', '5', '--json']
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 or not result.stdout.strip():
            return []
        payload = json.loads(result.stdout)
        return payload.get("results", [])
    except Exception:
        return []


def check_result_direct(results, expected_source, expected_content):
    for i, r in enumerate(results[:3]):
        source_match = expected_source.lower() in r.get('source', '').lower()
        content_match = expected_content.lower() in r.get('content', '').lower()
        if source_match or content_match:
            return True, i + 1
    return False, -1


def check_result_unified(results, expected_source, expected_content):
    for i, r in enumerate(results[:3]):
        source_match = expected_source.lower() in (r.get('path', '') + r.get('title', '')).lower()
        content_match = expected_content.lower() in r.get('content', '').lower()
        if source_match or content_match:
            return True, i + 1
    return False, -1


def main():
    tests = json.loads(EVAL_SET.read_text())
    passed = 0
    failed = 0
    slow = 0
    results_log = []

    for t in tests:
        q = t['query']
        col = t['collection']
        exp_src = t['expected_source']
        exp_content = t['expected_content']

        start = time.time()
        try:
            if col == "unified":
                results = run_query_unified(q)
                found, rank = check_result_unified(results, exp_src, exp_content)
            else:
                results = run_query_direct(q, col)
                found, rank = check_result_direct(results, exp_src, exp_content)
        except Exception as e:
            found, rank = False, -1
            results = []
        elapsed = time.time() - start

        status = f"PASS rank #{rank}" if found else "FAIL"
        latency_warn = " [SLOW]" if elapsed > 2.0 else ""
        if found:
            passed += 1
        else:
            failed += 1
        if elapsed > 2.0:
            slow += 1

        results_log.append({
            'query': q,
            'collection': col,
            'status': status,
            'latency_ms': round(elapsed * 1000),
            'actual_top': (results[0].get('source', '') or results[0].get('path', '')) if results else 'no results',
        })

        print(f"  {status}{latency_warn} ({elapsed:.1f}s) | [{col}] \"{q}\"")
        if not found and results:
            print(f"         Expected: {exp_src} / {exp_content}")

    total = passed + failed
    accuracy = (passed / total * 100) if total else 0

    print(f"\n{'=' * 50}")
    print(f"Results: {passed}/{total} ({accuracy:.0f}%)")
    print(f"Passed: {passed} | Failed: {failed} | Slow (>2s): {slow}")
    print(f"{'=' * 50}")

    report_path = Path('/Users/chrischo/server/brain/logs') / 'eval-report.json'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        'timestamp': str(datetime.now()),
        'passed': passed,
        'failed': failed,
        'accuracy': accuracy,
        'slow_count': slow,
        'details': results_log,
    }, indent=2, ensure_ascii=False))
    print(f"Report saved: {report_path}")

    # Append to eval history JSONL for regression tracking
    history_path = Path('/Users/chrischo/server/brain/logs') / 'eval-history.jsonl'
    failed_queries = [r['query'] for r in results_log if 'FAIL' in r.get('status', '')]
    with open(history_path, 'a') as hf:
        hf.write(json.dumps({
            'timestamp': datetime.now().isoformat(),
            'total': total,
            'passed': passed,
            'failed': failed,
            'accuracy': round(accuracy, 1),
            'slow_count': slow,
            'failed_queries': failed_queries,
        }, ensure_ascii=False) + '\n')
    print(f"History appended: {history_path}")

    if accuracy < 85:
        try:
            alert_msg = f"RAG eval: {passed}/{total} ({accuracy:.0f}%). {slow} slow queries. Check needed."
            subprocess.run(
                ['/Users/chrischo/.local/bin/openclaw', 'message', 'send',
                 '--channel', 'telegram', '--target', '8484060831',
                 '--account', 'ellie-bot', '--message', alert_msg],
                timeout=30, capture_output=True
            )
        except Exception:
            pass


if __name__ == '__main__':
    main()
