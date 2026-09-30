from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import ObservedSurvey, QuestionMatch, ScenarioResult, TestScenario


class PlatformAdapter(ABC):
    name: str

    @classmethod
    @abstractmethod
    def supports(cls, url: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def discover(self, url: str) -> ObservedSurvey:
        raise NotImplementedError

    @abstractmethod
    async def run_scenario(
        self,
        url: str,
        scenario: TestScenario,
        matches: list[QuestionMatch],
        answer_code_map: dict[str, dict[str, str]] | None = None,
    ) -> ScenarioResult:
        raise NotImplementedError
