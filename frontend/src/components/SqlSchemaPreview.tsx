import { parseSqlSchemaPreview } from "../utils/sqlSchemaPreview";
import type { SqlScenario } from "../types/interview";

export type SqlSchemaPreviewProps = {
  sqlScenario: SqlScenario | null;
};

export function SqlSchemaPreview({ sqlScenario }: SqlSchemaPreviewProps) {
  if (!sqlScenario) return null;

  const tables = parseSqlSchemaPreview(sqlScenario.db_schema);

  return (
    <div className="editor" style={{ marginTop: 12 }}>
      <div className="editor-head">
        <div>
          <p className="label">SQL schema</p>
          <h5>Таблицы и поля для задания</h5>
          {sqlScenario.description && <p className="muted">{sqlScenario.description}</p>}
        </div>
      </div>

      {tables.length === 0 ? (
        <div className="log">
          <p>Не удалось разобрать структуру таблиц из db_schema.</p>
        </div>
      ) : (
        <div className="sql-schema-list">
          {tables.map((table) => (
            <div key={table.name} className="sql-schema-card">
              <h6>{table.name}</h6>

              <div style={{ overflowX: "auto" }}>
                <table className="result-table">
                  <thead>
                    <tr>
                      <th>Поле</th>
                      <th>Тип</th>
                    </tr>
                  </thead>
                  <tbody>
                    {table.columns.map((col) => (
                      <tr key={`${table.name}-${col.name}`}>
                        <td>{col.name}</td>
                        <td>{col.type}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {table.sampleColumns.length > 0 && (
                <>
                  <p className="label" style={{ marginTop: 12 }}>Пример данных</p>
                  <div style={{ overflowX: "auto" }}>
                    <table className="result-table">
                      <thead>
                        <tr>
                          {table.sampleColumns.map((col) => (
                            <th key={`${table.name}-${col}`}>{col}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {table.sampleRows.length ? (
                          table.sampleRows.map((row, idx) => (
                            <tr key={`${table.name}-row-${idx}`}>
                              {row.map((cell, cellIdx) => (
                                <td key={`${table.name}-${idx}-${cellIdx}`}>{cell}</td>
                              ))}
                            </tr>
                          ))
                        ) : (
                          <tr>
                            <td colSpan={table.sampleColumns.length}>Нет примеров строк</td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}