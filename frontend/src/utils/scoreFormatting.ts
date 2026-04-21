function splitImprovementItems(value: string): string[] {
  const raw = (value || "").trim();
  if (!raw) return [];

  const numbered = raw.match(/\d+\)\s*.*?(?=(?:\s+\d+\)\s*)|$)/g);
  if (numbered && numbered.length) {
    return numbered.map((item) => item.trim());
  }

  if (raw.includes(";")) {
    return raw
      .split(";")
      .map((item) => item.trim())
      .filter(Boolean);
  }

  return [raw];
}

const LEADING_SCORE_LINE_RE = /^\s*(?:\*\*)?(?:оценка|балл)(?:\*\*)?\s*:\s*(?:не выставлена.*|\d+(?:[.,]\d+)?\s*\/\s*\d+)\s*$/i;
const LEADING_POINTS_LINE_RE = /^\s*points?\s*:\s*\d+(?:[.,]\d+)?(?:\s*\/\s*\d+)?\s*$/i;
const LEADING_COMMENT_LABEL_RE = /^\s*(?:comment|комментарий)\s*:\s*(.*)\s*$/i;

export function normalizePracticeReply(reply: string): string {
  const lines = String(reply || "").split("\n");

  while (lines.length) {
    const first = lines[0]?.trim() || "";
    if (!first) {
      lines.shift();
      continue;
    }

    if (LEADING_SCORE_LINE_RE.test(first) || LEADING_POINTS_LINE_RE.test(first)) {
      lines.shift();
      continue;
    }

    const commentMatch = first.match(LEADING_COMMENT_LABEL_RE);
    if (commentMatch) {
      const tail = (commentMatch[1] || "").trim();
      if (tail) {
        lines[0] = tail;
      } else {
        lines.shift();
      }
      continue;
    }

    break;
  }

  return lines.join("\n").trim();
}

function buildFormattedComment(comment: string, headers: string[], mainHeaders: string[]): string {
  const raw = (comment || "").trim();
  if (!raw) return raw;

  const lines = raw
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

  const sections = new Map<string, string>();
  let currentHeader: string | null = null;

  for (const line of lines) {
    const matchedHeader = headers.find((header) => line.startsWith(header));

    if (matchedHeader) {
      currentHeader = matchedHeader;
      sections.set(matchedHeader, line.slice(matchedHeader.length).trim());
      continue;
    }

    if (currentHeader) {
      const prev = sections.get(currentHeader) || "";
      sections.set(currentHeader, `${prev} ${line}`.trim());
    }
  }

  const md: string[] = [];
  const bodyBullets: string[] = [];

  for (const header of mainHeaders) {
    const value = sections.get(header);
    if (!value) continue;
    bodyBullets.push(`- **${header.replace(":", "")}:** ${value}`);
  }

  const improvements = sections.get("Что можно улучшить:");
  if (improvements) {
    const items = splitImprovementItems(improvements);
    if (items.length > 1) {
      bodyBullets.push(`- **Что можно улучшить:**`);
      for (const item of items) {
        bodyBullets.push(`  - ${item}`);
      }
    } else {
      bodyBullets.push(`- **Что можно улучшить:** ${improvements}`);
    }
  }

  if (bodyBullets.length) {
    md.push(bodyBullets.join("\n"));
  }

  return md.join("\n\n");
}

export function formatCodeScoreComment(comment: string): string {
  const headers = [
    "Корректность:",
    "Качество кода:",
    "Сложность и эффективность:",
    "Что можно улучшить:",
  ];

  const mainHeaders = [
    "Корректность:",
    "Качество кода:",
    "Сложность и эффективность:",
  ];

  return buildFormattedComment(comment, headers, mainHeaders);
}

export function formatSqlScoreComment(comment: string): string {
  const headers = [
    "Корректность:",
    "Качество решения:",
    "Работа с SQL:",
    "Что можно улучшить:",
  ];

  const mainHeaders = [
    "Корректность:",
    "Качество решения:",
    "Работа с SQL:",
  ];

  return buildFormattedComment(comment, headers, mainHeaders);
}
