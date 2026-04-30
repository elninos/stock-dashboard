#!/usr/bin/env python3
"""Per-stock 종합 분석 CLI.

사용:
  python -m analysis.cli 090470                       # 종합 (narrative + statistical)
  python -m analysis.cli 010170 000250 090470 950160
  python -m analysis.cli --names 제이스로보틱스
  python -m analysis.cli 090470 --layer narrative     # 한 레이어만
"""
import os, sys, json, argparse, warnings
warnings.filterwarnings("ignore")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from file_io import load_json
from config import STOCK_MAP_FILE
from analysis import narrative, statistical, synthesis


REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
os.makedirs(REPORT_DIR, exist_ok=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("codes", nargs="*")
    p.add_argument("--names", nargs="*")
    p.add_argument("--layer", choices=["narrative","statistical","synthesis"],
                   default="synthesis")
    p.add_argument("--print", action="store_true")
    args = p.parse_args()

    smap = load_json(STOCK_MAP_FILE, default={})
    code_to_name = {info["code"]: name for name, info in smap.items() if "code" in info}

    targets = []
    for c in args.codes or []:
        targets.append((code_to_name.get(c, c), c))
    for n in args.names or []:
        info = smap.get(n)
        if info and "code" in info:
            targets.append((n, info["code"]))

    if not targets:
        p.error("종목 코드 또는 --names 필요")

    mod = {"narrative": narrative, "statistical": statistical, "synthesis": synthesis}[args.layer]

    for name, code in targets:
        print(f"\n[분석:{args.layer}] {name} ({code})")
        result = mod.analyze(code, name=name)
        report_md = mod.render_report(result)

        suffix = "" if args.layer == "synthesis" else f"_{args.layer}"
        as_of = result.get("as_of") or "x"
        fpath_md = os.path.join(REPORT_DIR, f"{name}_{code}_{as_of}{suffix}.md")
        with open(fpath_md, "w") as f: f.write(report_md)
        fpath_json = fpath_md.replace(".md", ".json")
        with open(fpath_json, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        print(f"  → {fpath_md}")

        if args.layer == "synthesis":
            c = result["combined"]
            print(f"  종합: {c['grade']} (신뢰도 {c['confidence']})")
        elif args.layer == "narrative":
            print(f"  사건추적: {result.get('verdict','')}")
        else:
            print(f"  통계: {result.get('grade','')}")

        if args.print:
            print()
            print(report_md)
            print()


if __name__ == "__main__":
    main()
