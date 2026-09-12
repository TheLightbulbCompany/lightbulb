/**
 * Detects file paths in agent message content and wraps them
 * in a custom markdown link format that the chat UI can intercept.
 *
 * Uses the custom scheme `isol8-file://` to mark workspace file links as
 * distinct from regular URLs. The reader side of that scheme
 * (`isWorkspaceFileLink` / `extractFilePath`) went with MessageList's
 * `onOpenFile` prop, which no caller ever passed — restore them from git
 * history if a surface ever wires file links to a viewer again.
 */

// Matches paths like: path/to/file.ext or ./path/to/file.ext
// Must contain at least one slash and end with a file extension.
// Excludes URLs (http://, https://), package refs (@foo/bar), and common false positives.
const FILE_PATH_REGEX = /(?<![a-zA-Z]:\/\/|@)(?:\.\/)?(?:[a-zA-Z0-9_-]+\/)+[a-zA-Z0-9_.-]+\.[a-zA-Z0-9]{1,10}/g;

// Extensions that are almost certainly files, not package names or URLs
const FILE_EXTENSIONS = new Set([
  "md", "txt", "py", "js", "ts", "tsx", "jsx", "json", "yaml", "yml",
  "toml", "sh", "bash", "css", "html", "xml", "csv", "sql", "rs",
  "go", "java", "c", "cpp", "h", "hpp", "rb", "php", "swift", "kt",
  "r", "lua", "env", "cfg", "ini", "conf", "log", "png", "jpg",
  "jpeg", "gif", "svg", "webp", "pdf",
]);

/**
 * Pre-processes message content to convert detected file paths
 * into clickable markdown links with the isol8-file:// scheme.
 *
 * Example:
 *   "Plan written to isol8_agents/plan.md"
 *   → "Plan written to [isol8_agents/plan.md](isol8-file://isol8_agents/plan.md)"
 */
export function linkifyFilePaths(content: string): string {
  return content.replace(FILE_PATH_REGEX, (match) => {
    const ext = match.split(".").pop()?.toLowerCase();
    if (!ext || !FILE_EXTENSIONS.has(ext)) {
      return match;
    }
    // Don't double-wrap if already inside a markdown link
    return `[${match}](isol8-file://${match})`;
  });
}
