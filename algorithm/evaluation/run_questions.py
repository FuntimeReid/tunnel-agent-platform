"""Collect evidence API outputs. No scoring, automatic retries, or saved secrets."""
import argparse
import csv
import datetime as dt
import getpass
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.request


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cases(path):
    cases = read_json(path)["questions"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("questions must be a nonempty list")
    ids = set()
    for case in cases:
        key = case.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key) or key in ids:
            raise ValueError("Each question needs a unique ID, e.g. Q001")
        ids.add(key)
        if not isinstance(case.get("question"), str) or not case["question"].strip():
            raise ValueError("Empty question: " + key)
    return cases


def make_payload(case, args):
    # Reference answers, scoring rules, and evidence NEVER enter the request.
    payload = {
        "question": case["question"], "dataset_ids": [args.dataset_id],
        "mode": args.current_mode, "output": args.output,
        "top_n": args.top_n, "max_tokens": args.max_tokens,
        "timeout_seconds": 900, "diagnostics": True,
    }
    if "doc_ids" in case:
        payload["doc_ids"] = case["doc_ids"]
    return payload


def export_summary(root):
    columns = ["question_id", "mode", "output", "http", "code", "source_count",
               "client_seconds", "server_seconds", "research_seconds", "answer_seconds",
               "chat_tokens", "chat_calls", "revision", "model", "context_omitted_chunks",
               "answer_may_be_truncated"]
    with (root / "调用统计.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        for path in sorted(root.glob("*.result.json")):
            record = read_json(path)
            body = record.get("response") or {}
            data = body.get("data") or {}
            usage = data.get("usage") or body.get("usage") or {}
            writer.writerow({
                "question_id": record["question_id"], "mode": record["request"]["mode"],
                "output": record["request"]["output"], "http": record["http"],
                "code": body.get("code"), "source_count": len(data.get("sources") or []),
                "client_seconds": record["client_seconds"],
                "server_seconds": data.get("elapsed_seconds", body.get("elapsed_seconds")),
                "research_seconds": data.get("research_seconds"),
                "answer_seconds": data.get("answer_seconds"),
                "chat_tokens": usage.get("total_tokens"), "chat_calls": usage.get("calls"),
                "revision": data.get("implementation_revision"),
                "model": json.dumps(data.get("model"), ensure_ascii=False),
                "context_omitted_chunks": data.get("context_omitted_chunks"),
                "answer_may_be_truncated": data.get("answer_may_be_truncated"),
            })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:29390")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--modes", nargs="+", choices=["standard", "medium", "high"],
                        default=["standard", "medium", "high"])
    parser.add_argument("--ids", nargs="+", help="Pilot or explicit retry IDs")
    parser.add_argument("--output", choices=["answer", "chunks"], default="answer")
    parser.add_argument("--top-n", type=int, default=12)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cases = load_cases(args.cases)
    if args.ids:
        unknown = set(args.ids) - {case["id"] for case in cases}
        if unknown:
            raise ValueError("Unknown IDs: " + ", ".join(sorted(unknown)))
        cases = [case for case in cases if case["id"] in args.ids]
    jobs = []
    for case in cases:
        for mode in dict.fromkeys(args.modes):
            args.current_mode = mode
            jobs.append((case["id"], make_payload(case, args)))
    if args.dry_run:
        print(json.dumps({"planned_requests": len(jobs),
                          "requests": [{"id": key, "body": body} for key, body in jobs]},
                         ensure_ascii=False, indent=2))
        return

    root = args.out
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "dataset_id": args.dataset_id, "base_url": args.base_url.rstrip("/"),
        "modes": list(dict.fromkeys(args.modes)), "output": args.output,
        "top_n": args.top_n, "max_tokens": args.max_tokens,
    }
    manifest_path = root / "run_config.json"
    if manifest_path.exists() and read_json(manifest_path) != manifest:
        raise ValueError("Config or questions changed. Choose a NEW --out folder.")
    write_json(manifest_path, manifest)
    pending = []
    for key, payload in jobs:
        stem = key + "_" + payload["mode"]
        if (root / (stem + ".sent.json")).exists():
            raise RuntimeError("Unconfirmed prior request: " + stem + ". Check before retrying.")
        if not (root / (stem + ".result.json")).exists():
            pending.append((key, payload, stem))
    print(f"Planned: {len(jobs)}; already recorded: {len(jobs)-len(pending)}; to send: {len(pending)}")
    if not pending:
        export_summary(root)
        return
    base = args.base_url.rstrip("/")
    with urllib.request.urlopen(base + "/api/v1/system/healthz", timeout=15) as response:
        if response.status != 200:
            raise RuntimeError("Health check failed; no model requests sent")
    auth = getpass.getpass("Paste YOUR RAGFlow Authorization value (hidden, not saved): ").strip()
    if not auth:
        raise ValueError("Authorization is empty")
    try:
        for key, payload, stem in pending:
            marker = root / (stem + ".sent.json")
            write_json(marker, {"question_id": key, "request": payload,
                                "started_utc": dt.datetime.now(dt.timezone.utc).isoformat()})
            req = urllib.request.Request(base + "/api/v1/algorithm/evidence",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json", "Authorization": auth})
            started = time.monotonic()
            try:
                try:
                    with urllib.request.urlopen(req, timeout=940) as response:
                        http, raw = response.status, response.read()
                except urllib.error.HTTPError as error:
                    http, raw = error.code, error.read()
                body = json.loads(raw.decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError("Response is not a JSON object")
            except Exception as error:
                write_json(root / (stem + ".uncertain.json"),
                           {"question_id": key, "error_type": type(error).__name__,
                            "note": "Request may have run. Marker retained; no automatic retry."})
                raise RuntimeError("Request result unknown: " + stem + "; check with maintainer") from None
            record = {"question_id": key, "request": payload, "http": http, "response": body,
                      "client_seconds": round(time.monotonic() - started, 3),
                      "completed_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
            write_json(root / (stem + ".result.json"), record)
            marker.unlink()
            data = body.get("data") or {}
            print(f"{stem}: HTTP {http}, code={body.get('code')}, "
                  f"sources={len(data.get('sources') or [])}, {record['client_seconds']}s")
            if http != 200 or body.get("code") != 0:
                print("Stopped after an API error. Keep results; diagnose before sending more requests.")
                break
    finally:
        export_summary(root)


if __name__ == "__main__":
    main()
