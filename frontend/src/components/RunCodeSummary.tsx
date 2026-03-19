import type { SandboxRunResult } from "../types/interview";

export type RunCodeSummaryProps = {
  runCodeResult: SandboxRunResult | null;
};

export function RunCodeSummary({ runCodeResult }: RunCodeSummaryProps) {
  if (!runCodeResult) return null;

  const total = Number(runCodeResult.tests_total || 0);
  const passed = Number(runCodeResult.tests_passed || 0);
  const tests = Array.isArray(runCodeResult.test_results) ? runCodeResult.test_results : [];

  return (
    <div className="log">
      <p className="label">Результат песочницы</p>
      <p>
        Пройдено <strong>{passed}</strong> из <strong>{total}</strong> тестов
      </p>

      {tests.length > 0 && (
        <div style={{ display: "grid", gap: 8 }}>
          {tests.map((test, idx) => {
            const hasError = Boolean(test.error);

            return (
              <div key={`${test.code || idx}`} className="message system">
                <div className="message-meta">
                  <span>{test.name || test.code || `test_${idx + 1}`}</span>
                  <span className="pill small">{test.passed ? "passed" : "failed"}</span>
                </div>
                {hasError && <p>Ошибка: {String(test.error)}</p>}

                {!hasError && test.validation_mode === "custom_checker" && (
                  <>
                    <p>Проверка: кастомное правило</p>
                    <p>Actual: {JSON.stringify(test.actual)}</p>
                  </>
                )}

                {!hasError && test.validation_mode === "expected_error" && (
                  <>
                    <p>Expected error: {JSON.stringify(test.expected)}</p>
                    <p>Actual: {JSON.stringify(test.actual)}</p>
                  </>
                )}

                {!hasError &&
                  (!test.validation_mode || test.validation_mode === "exact") && (
                    <>
                      <p>Expected: {JSON.stringify(test.expected)}</p>
                      <p>Actual: {JSON.stringify(test.actual)}</p>
                    </>
                  )}
              </div>
            );
          })}
        </div>
      )}

      {(runCodeResult.stderr || runCodeResult.stdout) && (
        <div style={{ marginTop: 12 }}>
          {runCodeResult.stderr && (
            <>
              <p className="label">stderr</p>
              <pre>{runCodeResult.stderr}</pre>
            </>
          )}
          {runCodeResult.stdout && (
            <>
              <p className="label">stdout</p>
              <pre>{runCodeResult.stdout}</pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}
