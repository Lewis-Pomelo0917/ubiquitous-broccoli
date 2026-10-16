from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .core import State, collect_events, export_feed, load_sources, render_digest


def main() -> int:
    # Codex and many Windows terminals consume UTF-8, while the process may
    # otherwise inherit a legacy code page that cannot represent source text.
    if hasattr(sys.stdout, "reconfigure"):
        # Line buffering keeps --watch progress visible when output is piped to a log.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    parser = argparse.ArgumentParser(description="Rules-first AI intelligence radar MVP")
    parser.add_argument("--db", default="ai_radar.db", help="SQLite state path")
    parser.add_argument("--sources", default=str(Path(__file__).with_name("sources.json")))
    parser.add_argument("--once", action="store_true", help="Fetch sources once")
    parser.add_argument("--digest", action="store_true", help="Print recent events")
    parser.add_argument("--watch", action="store_true", help="Poll sources continuously without calling an LLM")
    parser.add_argument("--interval", type=int, default=300, help="Watch interval in seconds (default: 300)")
    parser.add_argument("--run-timeout", type=int, default=3000, help="Maximum seconds per collection cycle (default: 3000)")
    parser.add_argument("--source-timeout", type=int, default=60, help="Maximum seconds per source, including fallback (default: 60)")
    parser.add_argument("--source-id", help=argparse.SUPPRESS)
    parser.add_argument("--backfill-days", type=int, default=0, help="On first snapshot, include dated entries from the last N days")
    parser.add_argument("--daily", action="store_true", help="Render events discovered today in the selected timezone")
    parser.add_argument("--timezone", default="Asia/Singapore", help="IANA timezone for daily reports")
    parser.add_argument("--export-json", metavar="PATH", help="Atomically write the local JSON intelligence feed")
    args = parser.parse_args()
    state = State(args.db)
    try:
        sources = load_sources(args.sources)
        if args.source_id:
            source = next(source for source in sources if source.id == args.source_id)
            collect_events(source, state, backfill_days=args.backfill_days, force=args.once)
            return 0
        found = []
        cycle_started = None
        timed_out = []
        while True:
            if args.once or not args.digest or args.watch:
                found = []
                timed_out = []
                cycle_started = datetime.now(timezone.utc)
                deadline = time.monotonic() + max(60, args.run_timeout)
                for index, source in enumerate(sources):
                    if time.monotonic() >= deadline:
                        remaining = sources[index:]
                        timed_out.extend(pending.id for pending in remaining)
                        print(f"达到本轮 {max(60, args.run_timeout)} 秒上限，剩余 {len(remaining)} 个来源未检查；将导出已完成结果。")
                        break
                    remaining_seconds = max(1, int(deadline - time.monotonic()))
                    command = [
                        sys.executable, "-m", "ai_radar", "--db", args.db,
                        "--sources", args.sources, "--source-id", source.id,
                    ]
                    if args.once:
                        command.append("--once")
                    if args.backfill_days:
                        command.extend(["--backfill-days", str(args.backfill_days)])
                    try:
                        completed = subprocess.run(
                            command, cwd=str(Path.cwd()), capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=max(1, min(args.source_timeout, remaining_seconds)),
                        )
                        if completed.returncode:
                            state.record_error(source.id, f"worker exit {completed.returncode}: {completed.stderr[-200:]}")
                            print(f"来源失败：{source.name}（worker exit {completed.returncode}）")
                            continue
                    except subprocess.TimeoutExpired:
                        state.record_error(source.id, "timeout: source worker exceeded its time budget")
                        print(f"来源超时：{source.name}；继续检查后续来源。")
                found = state.events_since(cycle_started)
                if args.watch and found:
                    print(render_digest(found))
                if args.export_json:
                    export_feed(state, args.export_json, (source.id for source in sources), extra_failed=timed_out, checked_since=cycle_started)
                    if not args.watch:
                        print(f"已导出 AI 情报：{args.export_json}")
            if not args.watch:
                break
            time.sleep(max(30, args.interval))
        if args.daily:
            local_now = datetime.now(ZoneInfo(args.timezone))
            local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            print(render_digest(state.events_since(local_midnight), max_items=20))
        elif args.digest:
            print(render_digest(found if args.once or args.watch else state.recent_events()))
        if args.export_json and cycle_started is None:
            export_feed(state, args.export_json, (source.id for source in sources))
            print(f"已导出 AI 情报：{args.export_json}")
        if args.daily or args.digest:
            health = state.health_summary((source.id for source in sources), since=cycle_started)
            print(
                f"\n覆盖状态：成功 {health['ok']}/{health['total']}｜"
                f"失败 {health['failed']}｜待检查 {health['pending']}"
            )
            if health["failed"]:
                for source_id, message in state.failed_sources(limit=len(sources), since=cycle_started):
                    print(f"- 失败来源 {source_id}：{message}")
            for source_id in timed_out:
                print(f"- 未检查来源 {source_id}：本轮总时限已到")
    finally:
        state.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
