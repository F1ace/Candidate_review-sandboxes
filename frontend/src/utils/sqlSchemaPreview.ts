export type SqlColumnPreview = {
  name: string;
  type: string;
};

export type SqlTablePreview = {
  name: string;
  columns: SqlColumnPreview[];
  sampleColumns: string[];
  sampleRows: string[][];
};

function splitSqlValueGroups(valuesBlock: string): string[] {
  const groups: string[] = [];
  let depth = 0;
  let current = "";

  for (const ch of valuesBlock) {
    if (ch === "(") depth += 1;
    if (ch === ")") depth -= 1;

    current += ch;

    if (depth === 0 && current.trim()) {
      groups.push(current.trim().replace(/,$/, ""));
      current = "";
    }
  }

  return groups.filter(Boolean);
}

function splitCsvRespectingQuotes(input: string): string[] {
  const parts: string[] = [];
  let current = "";
  let inQuote = false;

  for (let i = 0; i < input.length; i += 1) {
    const ch = input[i];

    if (ch === "'" && input[i - 1] !== "\\") {
      inQuote = !inQuote;
      current += ch;
      continue;
    }

    if (ch === "," && !inQuote) {
      parts.push(current.trim());
      current = "";
      continue;
    }

    current += ch;
  }

  if (current.trim()) parts.push(current.trim());
  return parts;
}

export function parseSqlSchemaPreview(dbSchema?: string | null): SqlTablePreview[] {
  if (!dbSchema) return [];

  const tables = new Map<string, SqlTablePreview>();

  const createTableRegex = /CREATE TABLE\s+(\w+)\s*\(([\s\S]*?)\);/gi;
  let createMatch: RegExpExecArray | null;

  while ((createMatch = createTableRegex.exec(dbSchema)) !== null) {
    const tableName = createMatch[1];
    const body = createMatch[2];

    const columnLines = body
      .split("\n")
      .map((line) => line.trim().replace(/,$/, ""))
      .filter(Boolean)
      .filter(
        (line) =>
          !line.startsWith("PRIMARY KEY") &&
          !line.startsWith("FOREIGN KEY") &&
          !line.startsWith("UNIQUE") &&
          !line.startsWith("CONSTRAINT"),
      );

    const columns = columnLines.map((line) => {
      const [name, ...rest] = line.split(/\s+/);
      return {
        name,
        type: rest.join(" "),
      };
    });

    tables.set(tableName, {
      name: tableName,
      columns,
      sampleColumns: [],
      sampleRows: [],
    });
  }

  const insertRegex = /INSERT INTO\s+(\w+)\s*\((.*?)\)\s*VALUES\s*([\s\S]*?);/gi;
  let insertMatch: RegExpExecArray | null;

  while ((insertMatch = insertRegex.exec(dbSchema)) !== null) {
    const tableName = insertMatch[1];
    const columnBlock = insertMatch[2];
    const valuesBlock = insertMatch[3];

    const table = tables.get(tableName);
    if (!table) continue;

    const sampleColumns = splitCsvRespectingQuotes(columnBlock).map((item) =>
      item.replace(/"/g, "").trim(),
    );
    const valueGroups = splitSqlValueGroups(valuesBlock).slice(0, 5);

    const sampleRows = valueGroups.map((group) => {
      const inner = group.trim().replace(/^\(/, "").replace(/\)$/, "");
      return splitCsvRespectingQuotes(inner).map((value) => value.trim());
    });

    table.sampleColumns = sampleColumns;
    table.sampleRows = sampleRows;
  }

  return Array.from(tables.values());
}