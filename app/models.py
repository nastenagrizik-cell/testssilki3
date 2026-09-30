from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class QuestionType(str, Enum):
    UNKNOWN = "unknown"
    SINGLE = "single"
    MULTI = "multi"
    TEXT = "text"
    NUMBER = "number"
    SCALE = "scale"
    RANKING = "ranking"
    MATRIX_SINGLE = "matrix_single"
    MATRIX_MULTI = "matrix_multi"
    INFO = "info"


class AnswerOption(BaseModel):
    code: str
    text: str
    exclusive: bool = False
    fixed: bool = False


class ConditionAtom(BaseModel):
    question_id: str
    operator: Literal["eq", "ne", "in", "not_in"] = "in"
    values: list[str] = Field(default_factory=list)


class ConditionGroup(BaseModel):
    combinator: Literal["and", "or"] = "and"
    atoms: list[ConditionAtom] = Field(default_factory=list)


class LogicRule(BaseModel):
    action: Literal["show", "hide", "continue", "terminate", "rotate", "other"]
    target_question_id: str | None = None
    condition: ConditionGroup | None = None
    raw_text: str
    confidence: float = 0.5
    source_block: int | None = None


class RotationSpec(BaseModel):
    enabled: bool = False
    axis: Literal["options", "rows", "columns", "questions", "concepts", "unknown"] = "unknown"
    included_codes: list[str] = Field(default_factory=list)
    fixed_codes: list[str] = Field(default_factory=list)
    raw_text: str | None = None


class SourceLocation(BaseModel):
    block_index: int
    kind: str
    style: str | None = None


class QuestionSpec(BaseModel):
    id: str
    text: str
    type: QuestionType = QuestionType.UNKNOWN
    options: list[AnswerOption] = Field(default_factory=list)
    matrix_rows: list[AnswerOption] = Field(default_factory=list)
    required: bool | None = None
    rotation: RotationSpec | None = None
    display_rules: list[LogicRule] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    source: SourceLocation


class SurveySpec(BaseModel):
    title: str
    questions: list[QuestionSpec] = Field(default_factory=list)
    rules: list[LogicRule] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObservedQuestion(BaseModel):
    platform_id: str
    reference_code: str | None = None
    text: str
    type: QuestionType = QuestionType.UNKNOWN
    raw_type: str | None = None
    options: list[AnswerOption] = Field(default_factory=list)
    matrix_rows: list[AnswerOption] = Field(default_factory=list)
    position: int = 0
    option_order_samples: list[list[str]] = Field(default_factory=list)
    row_order_samples: list[list[str]] = Field(default_factory=list)


class ObservedSurvey(BaseModel):
    platform: str
    source_url: str
    questions: list[ObservedQuestion] = Field(default_factory=list)
    question_order_samples: list[list[str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class QuestionMatch(BaseModel):
    expected_id: str
    observed_id: str
    score: float
    expected_position: int
    observed_position: int


class Issue(BaseModel):
    code: str
    severity: Literal["critical", "major", "minor", "warning"]
    title: str
    expected: str | None = None
    actual: str | None = None
    question_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class TestScenario(BaseModel):
    id: str
    label: str
    assignments: dict[str, list[str]] = Field(default_factory=dict)
    expected_visible: list[str] = Field(default_factory=list)
    expected_hidden: list[str] = Field(default_factory=list)
    expected_texts: dict[str, str] = Field(default_factory=dict)
    source_rule: str | None = None


class ScenarioResult(BaseModel):
    scenario_id: str
    completed: bool
    visited_platform_ids: list[str] = Field(default_factory=list)
    visited_expected_ids: list[str] = Field(default_factory=list)
    observed_questions: list[ObservedQuestion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ComparisonReport(BaseModel):
    created_at: str
    document_title: str
    platform: str
    source_url: str
    expected_question_count: int
    observed_question_count: int
    matches: list[QuestionMatch] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    scenarios: list[TestScenario] = Field(default_factory=list)
    scenario_results: list[ScenarioResult] = Field(default_factory=list)
    parser_warnings: list[str] = Field(default_factory=list)
    platform_warnings: list[str] = Field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(issue.severity == "critical" for issue in self.issues)
