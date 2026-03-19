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

export function formatScoreComment(comment: string): string {
  const raw = (comment || "").trim();
  if (!raw) return raw;

  const headers = [
    "Корректность:",
    "Качество кода:",
    "Сложность и эффективность:",
    "Что можно улучшить:",
  ];

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

  for (const header of [
    "Корректность:",
    "Качество кода:",
    "Сложность и эффективность:",
  ]) {
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

