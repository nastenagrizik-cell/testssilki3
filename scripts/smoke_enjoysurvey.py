from __future__ import annotations

import argparse
import asyncio
import json

from app.platforms.enjoysurvey import EnjoySurveyAdapter
from app.url_security import validate_public_url


async def run(url: str, summary: bool = False) -> None:
    survey = await EnjoySurveyAdapter(headless=True).discover(validate_public_url(url))
    if summary:
        print(
            json.dumps(
                {
                    "platform": survey.platform,
                    "question_count": len(survey.questions),
                    "probe_runs": len(survey.question_order_samples),
                    "questions_with_options": sum(bool(question.options) for question in survey.questions),
                    "warnings": survey.warnings,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(survey.model_dump_json(indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read the structure of an EnjoySurvey test link.")
    parser.add_argument("url")
    parser.add_argument("--summary", action="store_true", help="Print only counts and warnings")
    args = parser.parse_args()
    asyncio.run(run(args.url, summary=args.summary))
