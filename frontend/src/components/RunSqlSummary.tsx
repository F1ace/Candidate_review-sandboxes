import type { SqlRunResult } from "../types/interview";

export type RunSqlSummaryProps = {
  sqlRunResult: SqlRunResult | null;
};

export function RunSqlSummary({ sqlRunResult }: RunSqlSummaryProps) {
  if (!sqlRunResult) return null;

  const result =
    sqlRunResult && typeof sqlRunResult === "object" && sqlRunResult.result
      ? sqlRunResult.result
      : null;

  const success = Boolean(result?.success);
  const columns = Array.isArray(result?.columns) ? result.columns : [];
  const rows = Array.isArray(result?.rows) ? result.rows : [];
  const error = result?.error ? String(result.error) : "";

  return (
    <div className="log">
      <p className="label">Результат SQL-песочницы</p>

      <p>
        Статус: <strong>{success ? "успешно" : "ошибка"}</strong>
      </p>

      {sqlRunResult.sql_scenario_id && (
        <p>
          Сценарий: <strong>{sqlRunResult.sql_scenario_id}</strong>
        </p>
      )}

      {error && (
        <div className="message system">
          <div className="message-meta">
            <span>SQL error</span>
          </div>
          <pre>{error}</pre>
        </div>
      )}

      {!error && columns.length > 0 && (
        <div style={{ overflowX: "auto", marginTop: 12 }}>
          <table className="result-table">
            <thead>
              <tr>
                {columns.map((col) => (
                  <th key={col}>{col}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.length ? (
                rows.map((row, rowIdx) => (
                  <tr key={rowIdx}>
                    {(Array.isArray(row) ? row : []).map((cell, cellIdx) => (
                      <td key={cellIdx}>
                        {cell === null ? "NULL" : String(cell)}
                      </td>
                    ))}
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={columns.length}>Запрос выполнен, но не вернул строк.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}