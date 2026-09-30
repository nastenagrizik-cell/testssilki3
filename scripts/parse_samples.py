from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.docx_parser import DocxSurveyParser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Parse DOCX questionnaires without uploading them.")
    parser.add_argument("path", type=Path, help="DOCX file or a folder containing DOCX files")
    parser.add_argument("--output", type=Path, default=Path("reports/parsed"))
    args = parser.parse_args()
    paths = sorted(args.path.glob("*.docx")) if args.path.is_dir() else [args.path]
    args.output.mkdir(parents=True, exist_ok=True)
    questionnaire_parser = DocxSurveyParser()
    for path in paths:
        spec = questionnaire_parser.parse(path)
        destination = args.output / f"{path.stem}.json"
        destination.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
        print(f"{path.name}: {len(spec.questions)} questions, {len(spec.rules)} rules -> {destination}")


if __name__ == "__main__":
    main()
