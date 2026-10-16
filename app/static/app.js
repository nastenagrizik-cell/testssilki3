const form = document.querySelector("#audit-form");
const formPanel = document.querySelector("#form-panel");
const progressPanel = document.querySelector("#progress-panel");
const resultPanel = document.querySelector("#result-panel");
const errorPanel = document.querySelector("#error-panel");
const stage = document.querySelector("#stage");
const severityLabels = {
  critical: "Критическая",
  major: "Существенная",
  minor: "Незначительная",
  warning: "Предупреждение",
};

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

function showError(message) {
  progressPanel.classList.add("hidden");
  resultPanel.classList.add("hidden");
  errorPanel.classList.remove("hidden");
  document.querySelector("#error-text").textContent = message;
  formPanel.classList.remove("hidden");
}

function renderReport(jobId, report) {
  progressPanel.classList.add("hidden");
  resultPanel.classList.remove("hidden");
  const counts = { critical: 0, major: 0, minor: 0, warning: 0 };
  for (const issue of report.issues) counts[issue.severity] += 1;
  document.querySelector("#summary").innerHTML = `
    <div class="metric"><strong>${report.expected_question_count}</strong><span>вопросов в Word</span></div>
    <div class="metric"><strong>${report.observed_question_count}</strong><span>элементов в ссылке</span></div>
    <div class="metric"><strong>${counts.critical}</strong><span>критических ошибок</span></div>
    <div class="metric"><strong>${report.scenario_results.length}</strong><span>проверенных маршрутов</span></div>
    ${report.expected_concept_count ? `<div class="metric"><strong>${report.observed_concept_count}/${report.expected_concept_count}</strong><span>покрыто концепций (${report.concept_attempts} прохожд.)</span></div>` : ""}`;
  const issues = document.querySelector("#issues");
  if (!report.issues.length) {
    issues.innerHTML = '<p class="empty">Расхождения не обнаружены.</p>';
  } else {
    issues.innerHTML = report.issues.map(issue => `
      <article class="issue">
        <span class="badge ${escapeHtml(issue.severity)}">${escapeHtml(severityLabels[issue.severity] || issue.severity)}</span>
        <h3>${escapeHtml(issue.question_id ? `${issue.question_id}: ${issue.title}` : issue.title)}</h3>
        ${issue.expected ? `<p><strong>Ожидалось:</strong> ${escapeHtml(issue.expected)}</p>` : ""}
        ${issue.actual ? `<p><strong>Фактически:</strong> ${escapeHtml(issue.actual)}</p>` : ""}
      </article>`).join("");
  }
  const warnings = [...(report.parser_warnings || []), ...(report.platform_warnings || [])];
  const warningPanel = document.querySelector("#warnings-panel");
  if (warnings.length) {
    warningPanel.classList.remove("hidden");
    document.querySelector("#warnings").innerHTML = warnings.map(item => `<li>${escapeHtml(item)}</li>`).join("");
  }
  document.querySelector("#download").href = `/api/jobs/${jobId}/report.json`;
}

async function poll(jobId) {
  for (;;) {
    const response = await fetch(`/api/jobs/${jobId}`);
    const job = await response.json();
    if (response.status === 404) {
      const error = new Error(job.detail || "Сервер потерял временное задание");
      error.jobWasLost = true;
      throw error;
    }
    if (!response.ok) throw new Error(job.detail || "Не удалось получить статус");
    stage.textContent = job.stage;
    if (job.status === "completed") return renderReport(jobId, job.result);
    if (job.status === "failed") throw new Error(job.error || "Проверка завершилась ошибкой");
    await new Promise(resolve => setTimeout(resolve, 1800));
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorPanel.classList.add("hidden");
  resultPanel.classList.add("hidden");
  formPanel.classList.add("hidden");
  progressPanel.classList.remove("hidden");
  stage.textContent = "Загрузка анкеты";
  try {
    // The selected File remains available in the form while this page stays
    // open, so after one Render restart we can safely submit the same audit
    // again without asking the user to choose the document a second time.
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const data = new FormData(form);
      data.set("run_logic", form.elements.run_logic.checked ? "true" : "false");
      data.set(
        "allow_platform_demographics",
        form.elements.allow_platform_demographics.checked ? "true" : "false",
      );
      const response = await fetch("/api/jobs", { method: "POST", body: data });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Не удалось запустить проверку");
      try {
        await poll(payload.job_id);
        return;
      } catch (error) {
        if (!error.jobWasLost || attempt > 0) throw error;
        stage.textContent = "Сервер перезапустился — автоматически запускаем проверку заново";
        await new Promise(resolve => setTimeout(resolve, 5000));
      }
    }
  } catch (error) {
    showError(error.message || String(error));
  }
});
